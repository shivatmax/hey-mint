"""Mint's own setup: what is set up on this Mac, what that lets it do, and Settings at the right place.

    "what can you do?"          setup_status(card=true): a card of what Mint does, grouped, each group marked
                                Ready or with what it still needs ("allow Calendar access", "connect Telegram")
    "is Telegram connected?"    setup_status(feature="telegram")
    "connect Telegram"          open_setup("telegram", reason=...): already set up -> nothing opens, Mint says so;
                                else Settings opens on its page (and part) and Mint says the steps, briefly

FEATURES is the setup guide: per thing to set up its id, the words people use for it, how its status is read
(each check is cheap, never prompts, and runs off the main thread), the Settings page and anchor, the steps in
a sentence or two, and what it enables. status() caches each answer a few seconds.

The permission statuses come from permissions.py (shared with the welcome window and Settings), the accounts
and Apple Shortcuts from connectors.py, Telegram and Email control from their bridges. The "what can you do"
card builds on the welcome window's tour (onboarding.TOUR): its features, each marked with what it needs;
tour=true opens that window ("What I can do", with clips).

Settings is opened through set_opener() (main.py registers presence.fire("open_settings", page, anchor)); with
no opener (the command line, tests) nothing opens and the steps say where to go.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("mint.tools.setup_guide")

TTL = 5.0                     # seconds a status is reused

# Settings pages (settings_window.PAGES) by key, used when that module can't be read.
PAGE_TITLES = {"general": "General", "voice": "Microphone & voice", "speaking": "Speaking",
               "looks": "Appearance & Sound", "shortcuts": "Shortcuts", "accounts": "Accounts & connections",
               "models": "Models & agents", "brain": "Skills & Memory", "storage": "Permissions & Privacy",
               "usage": "Usage", "help": "Updates & Help"}
# Parts of a page a feature would like to scroll to, but Settings has no anchor for (yet): the page opens at
# the top. {feature: (page, anchor wanted)}.
ANCHORS_WANTED = {"google": ("accounts", "google"), "google_meet": ("accounts", "meet"),
                  "claude_code": ("models", "claude_code"), "apps": ("accounts", "connectors"),
                  "voice_training": ("voice", "voice"), "typesafe_key": ("accounts", "keys")}

READY, PARTIAL, MISSING, UNAVAILABLE, UNKNOWN = "ready", "partial", "missing", "unavailable", "unknown"


@dataclass
class Feature:
    id: str
    title: str
    names: tuple                         # words people use for it (lower case); the id and title count too
    page: str                            # Settings page key
    anchor: str                          # a part of that page (settings_window._anchor), "" = the top
    steps: str                           # what to do there, in a sentence or two ({name}: the assistant's name)
    enables: str                         # what it lets the assistant do
    need: str                            # as a short "needs ..." on the card: "connect Telegram"
    check: Callable | None = None        # () -> dict(state, detail[, step, page, anchor])
    extra: dict = field(default_factory=dict)


# --- the checks (any thread, never prompt) -------------------------------------------------------------------

def _r(state: str, detail: str = "", **more) -> dict:
    return {"state": state, "detail": detail, **more}


def _env(*names: str):
    def check() -> dict:
        have = [n for n in names if os.environ.get(n, "").strip()]
        if have:
            return _r(READY, "Key added" + (f" ({len(have)} keys)" if len(have) > 1 else ""))
        return _r(MISSING, "No key yet")
    return check


_LABELS = {"ANTHROPIC_API_KEY": "Anthropic", "OPENROUTER_API_KEY": "OpenRouter", "GROQ_API_KEY": "Groq",
           "XAI_API_KEY": "xAI"}


def _other_keys() -> dict:
    have = [label for env, label in _LABELS.items() if os.environ.get(env, "").strip()]
    return _r(READY, ", ".join(have)) if have else _r(MISSING, "No other provider's key yet")


_asked: set = set()


def _permission(kind: str):
    def check() -> dict:
        from mint.core import permissions
        status = permissions.status(kind, _asked)
        if status == "allowed":
            return _r(READY, "Allowed")
        if status == "denied":
            return _r(MISSING, "Not allowed - it is switched off in System Settings",
                      step=f"Click Allow next to {permissions.title(kind)}: System Settings opens there - switch "
                           "{name} on.")
        return _r(MISSING, "Not allowed yet")
    return check


def _permissions_overall() -> dict:
    from mint.core import permissions
    statuses = {kind: permissions.status(kind, _asked) for kind in permissions.IMPORTANT}
    missing = permissions.missing_important(statuses)
    if not missing:
        return _r(READY, "The important ones are allowed")
    return _r(MISSING, "Not allowed: " + ", ".join(permissions.title(k) for k in missing))


def _connector(cid: str):
    def check() -> dict:
        from mint.tools import connectors
        found = connectors.get(cid)
        if found is None:
            return _r(UNKNOWN, "Can't check")
        status = found.status(False)
        state = {"connected": READY, "setup": MISSING, "missing": UNAVAILABLE}.get(status["state"], UNKNOWN)
        return _r(state, status["detail"])
    return check


def _google() -> dict:
    result = _connector("google")()
    if result["state"] == MISSING and "Allow Calendars" in result["detail"]:
        result["step"] = ("Click Connect Google and add your Google account in Internet Accounts with Mail and "
                          "Calendars on. Also allow Calendars for {name} in Permissions & Privacy.")
    return result


def _meet() -> dict:
    from mint.app import meet_call
    return _r(READY, "Signed in to Google") if meet_call.signed_in() else _r(MISSING, "Not signed in yet")


def _telegram() -> dict:
    from mint.app import telegram
    if not os.environ.get(telegram.TOKEN_ENV, "").strip():
        return _r(MISSING, "No bot token yet")
    state = telegram.status()
    if not state.get("enabled"):
        return _r(PARTIAL, "Bot token added, remote control is off",
                  step="In the Telegram card, switch Remote control on, then send your bot the pairing code shown "
                       "there from your phone.")
    if not state.get("paired"):
        return _r(PARTIAL, "On, but no phone paired yet",
                  step="From your phone, send your Telegram bot the pairing code shown in the Telegram card.")
    if state.get("error"):
        return _r(PARTIAL, f"Paired, but: {str(state['error'])[:80]}",
                  step="The Telegram card shows what's wrong - usually a new bot token fixes it.")
    who = (state.get("paired") or {}).get("name") or "your phone"
    return _r(READY, f"Paired with {who}" + (", read-only" if state.get("read_only") else ""))


def _email_control() -> dict:
    from mint.app import email_remote
    from mint.core import prefs
    if not prefs.get("email_enabled"):
        return _r(MISSING, "Off")
    state = email_remote.status()
    if state.get("error"):
        return _r(PARTIAL, f"On, but: {str(state['error'])[:80]}",
                  step="The Email control card says what's missing - fill that in.")
    return _r(READY, f"Watching {state.get('where') or state.get('address') or 'your inbox'}")


def _claude_code() -> dict:
    from mint.tools import agent_hooks
    from mint.tools import agent_watch
    if not agent_watch.installed().get("claude"):
        return _r(UNAVAILABLE, "Claude Code isn't installed on this Mac")
    if agent_hooks.installed():
        return _r(READY, "Connected: its questions and permission requests come to the notch")
    return _r(MISSING, "Installed, not connected")


def _voice() -> dict:
    from mint.voice import voicelock
    lock = voicelock.lock
    if getattr(lock, "stale", False):
        return _r(PARTIAL, "Needs training again (the voice model was upgraded)",
                  step="Under Your voice, click Retrain my voice and follow along - about two minutes.")
    if lock.enrolled:
        return _r(READY, f"Trained {getattr(lock, 'enrolled_at', '') or ''}".strip())
    return _r(MISSING, "Not trained yet")


def _dictation() -> dict:
    from mint.core import permissions
    missing = [k for k in ("input", "accessibility") if permissions.status(k, _asked) != "allowed"]
    if missing:
        names = " and ".join(permissions.title(k) for k in missing)
        return _r(MISSING, f"Needs {names}", page="storage", anchor="permissions", need=f"allow {names}",
                  step=f"Click Allow next to {names}, then switch {{name}} on in System Settings if it asks.")
    return _r(READY, "Ready: hold the dictate key, talk, let go")


def _apps() -> dict:
    from mint.tools import connectors
    rows = connectors.snapshot(deep=False, max_age=120.0)
    library = rows.get("library") or []
    on = sum(1 for r in library if r.get("state") == "connected") + len(rows.get("custom") or [])
    todo = sum(1 for r in library if r.get("state") == "setup")
    return _r(READY, f"{on} connected" + (f", {todo} could use a step" if todo else ""))


def _always(detail: str):
    return lambda: _r(READY, detail)


# --- the guide ----------------------------------------------------------------------------------------------

PERMISSION_STEPS = "Click Allow next to {title} and say OK when macOS asks (or switch {{name}} on in System Settings)."


def _perm_feature(kind: str, title: str, names: tuple, enables: str, need: str) -> Feature:
    return Feature(kind, title, names, "storage", "permissions", PERMISSION_STEPS.format(title=title), enables,
                   need, _permission(kind))


FEATURES: list[Feature] = [
    Feature("gemini_key", "Gemini key", ("gemini", "gemini key", "gemini api key", "google ai key", "ai studio",
                                         "api key", "second gemini key"),
            "models", "", "Under Gemini, click Add key next to Key 1 and paste a free key from "
                          "aistudio.google.com/apikey.",
            "{name}'s voice and thinking - nothing works without it.", "add a Gemini key",
            _env("GEMINI_API_KEY", "GEMINI_API_KEY_2")),
    Feature("typesafe_key", "TypeSafe key", ("typesafe", "type safe", "jev", "typesafe key", "typesafe api key"),
            "accounts", "", "Under API keys, click Add key next to TypeSafe and paste a key from "
                            "console.typesafe.ai/keys.",
            "Surer clicking and typing in any app, and quicker skill picks (optional).", "add a TypeSafe key",
            _env("TYPESAFE_API_KEY")),
    Feature("openai_key", "OpenAI key", ("openai", "open ai", "openai key", "openai api key", "chatgpt key",
                                         "gpt key", "chat gpt key"),
            "models", "model_keys", "Under AI models for your agents, click Add key next to OpenAI and paste a key "
                                    "from platform.openai.com/api-keys.",
            "Your agents can think with OpenAI's models (optional).", "add an OpenAI key", _env("OPENAI_API_KEY")),
    Feature("model_keys", "Other AI model keys", ("anthropic", "anthropic key", "claude key", "claude api key",
                                                  "groq", "openrouter", "open router", "xai", "grok",
                                                  "model key", "model provider", "other models", "ai models"),
            "models", "model_keys", "Under AI models for your agents, press More providers, then Add key next to "
                                    "the one you want and paste its key.",
            "Your agents can think with Claude, Groq, OpenRouter or xAI models (optional).",
            "add a model provider's key", _other_keys),
    Feature("google", "Google account (Gmail, Calendar)",
            ("google", "gmail", "google account", "google calendar", "google mail", "my email account",
             "email account", "mail account", "internet accounts", "outlook calendar"),
            "accounts", "", "Click Connect Google, add your Google account in Internet Accounts, and turn on Mail "
                            "and Calendars.",
            "Read, sort and draft your Gmail in Mail, and plan in your Google Calendar - all on this Mac.",
            "add your Google account", _google),
    Feature("google_meet", "Google Meet sign-in", ("google meet", "meet", "meet sign in", "video call",
                                                   "start a meet", "meet call"),
            "accounts", "", "Click Open sign-in next to Google Meet and sign in to Google in that window - you "
                            "type the password, not {name}. Then close it.",
            "\"Start a Google Meet\": {name} makes the call, sends you the link and talks with you in it.",
            "sign in to Google Meet", _meet),
    Feature("telegram", "Telegram", ("telegram", "telegram bot", "phone", "my phone", "connect my phone",
                                     "phone remote", "from my phone", "remote control", "control from phone",
                                     "botfather"),
            "accounts", "telegram", "In Telegram, message @BotFather, send /newbot and copy the token. Paste it "
                                    "next to Bot token here, then send your bot the pairing code shown.",
            "Message {name} on Telegram from anywhere: it works on the Mac and sends back each step and files.",
            "connect Telegram", _telegram),
    Feature("email_control", "Email control", ("email control", "control by email", "email remote",
                                               "email requests", "email me", "control from email",
                                               "email a request"),
            "accounts", "email", "Click Set up next to Email, switch Email control on and add your address (the "
                                 "one in Mail). Then email a request whose subject starts with “Mint:”.",
            "Email {name} a request from anywhere and get the answer by email.", "set up Email control",
            _email_control),
    Feature("claude_code", "Claude Code", ("claude code", "claude hooks", "claude code hooks", "coding agent",
                                           "approve from the notch", "claude in the notch", "claude"),
            "models", "", "Under Coding agents, click Connect next to Claude Code.",
            "Claude Code's questions and permission requests come to the notch: Allow or Deny from there.",
            "connect Claude Code", _claude_code),
    Feature("apple_shortcuts", "Apple Shortcuts", ("shortcut", "shortcuts", "apple shortcuts", "shortcuts app",
                                                   "siri shortcut", "make a shortcut", "image playground"),
            "shortcuts", "shortcuts", "Under Apple Shortcuts that {name} uses, click Add next to each one, then "
                                      "“Add Shortcut” in the Shortcuts app.",
            "Run your shortcuts by voice, make new ones from plain words, and Image Playground pictures.",
            "add {name}'s shortcuts", _connector("shortcuts")),
    Feature("voice_training", "Voice training", ("voice training", "train my voice", "train voice", "voice lock",
                                                 "voiceprint", "my voice", "recognize my voice", "voice trained"),
            "voice", "", "Under Your voice, click Train my voice: say “Hey {name}” eight times, then read eight "
                         "short sentences, in a quiet room.",
            "{name} wakes more surely for you, and the voice lock lets only you in.", "train your voice", _voice),
    Feature("dictation", "Dictation", ("dictation", "dictate", "voice typing", "dictate key"),
            "shortcuts", "", "Hold the dictate key, talk and let go; you can change the key here.",
            "Clean, punctuated text wherever your cursor is.", "allow Input Monitoring", _dictation),
    Feature("keyboard_shortcuts", "Keyboard shortcuts", ("keyboard shortcut", "keyboard shortcuts", "hotkey",
                                                         "hotkeys", "talk key", "shortcut key", "key combo"),
            "shortcuts", "", "Click a shortcut, then press the keys you want.",
            "Keys for the chat, talking, dictation and the clipboard, in any app.", "",
            _always("Set (you can change them)")),
    Feature("apps", "Apps and services", ("connectors", "apps and services", "integrations", "connected apps",
                                          "my apps"),
            "accounts", "", "Scroll down to Apps and services: each one says what it needs, with a Connect "
                            "button.",
            "Mail, Calendar, Notes, Music, Slack and other apps {name} works with.", "", _apps),
    Feature("permissions", "Permissions", ("permissions", "permission", "access", "privacy", "what access"),
            "storage", "permissions", "Click Allow next to each one that isn't allowed.",
            "What macOS lets {name} do: hear you, click and type, see the screen, read your calendar.",
            "allow permissions", _permissions_overall),
    _perm_feature("microphone", "Microphone", ("microphone", "mic", "microphone access"),
                  "Hearing the wake word and everything you ask.", "allow the Microphone"),
    _perm_feature("accessibility", "Accessibility", ("accessibility", "click and type", "control my mac"),
                  "Clicking, typing, scrolling and arranging windows for you.", "allow Accessibility"),
    _perm_feature("screen", "Screen Recording", ("screen recording", "screen access", "see my screen",
                                                 "screen permission", "screen capture"),
                  "Seeing your screen when you ask about it.", "allow Screen Recording"),
    _perm_feature("input", "Input Monitoring", ("input monitoring", "keyboard access"),
                  "The dictation key, and teaching {name} a task.", "allow Input Monitoring"),
    _perm_feature("calendar", "Calendars", ("calendar", "calendars", "calendar access", "my calendar", "events"),
                  "What's next today, and adding events.", "allow Calendar access"),
    _perm_feature("reminders", "Reminders", ("reminders", "reminder", "reminders access"),
                  "Reading and adding your reminders.", "allow Reminders"),
    _perm_feature("files", "Full Disk Access", ("full disk access", "disk access", "safari bookmarks"),
                  "Safari bookmarks, past notifications and other protected files.", "allow Full Disk Access"),
    _perm_feature("automation", "Automation", ("automation", "automation permission", "control apps",
                                               "apple events"),
                  "Working inside Notes, Mail, Music and other apps.", "allow Automation"),
]
_BY_ID = {f.id: f for f in FEATURES}

# The "what can you do" card: at most 7 rows (the island card's ROWS). (title, SF Symbol, what, features it needs,
# any=True: one of them is enough).
GROUPS = (
    ("Do things on your Mac", "cursorarrow.click.2", "Apps, clicks, typing, your screen",
     ("accessibility", "screen"), False),
    ("Your day", "calendar", "Calendar, reminders, Gmail", ("calendar", "reminders", "google"), False),
    ("From your phone", "paperplane.fill", "Telegram or email requests", ("telegram", "email_control"), True),
    ("Calls and meetings", "video.fill", "Google Meet, meeting notes", ("google_meet",), False),
    ("Coding agents", "chevron.left.forwardslash.chevron.right", "Claude Code in the notch, helper agents",
     ("claude_code",), False),
    ("Dictation and Shortcuts", "mic.fill", "Dictate anywhere, Apple Shortcuts, pictures",
     ("dictation", "apple_shortcuts"), False),
    ("Web, files and media", "globe", "Web tasks, documents, music, videos", (), False),
)
# The welcome window's tour (onboarding.TOUR, by title): what each feature needs set up first.
TOUR_NEEDS = {
    "Talk, and it's done": ("accessibility",), "Telegram, from your phone": ("telegram",),
    "Google Meet with me": ("google_meet",), "Email, handled": ("google",),
    "Claude Code in the notch": ("claude_code",), "Your day at a glance": ("calendar",),
    "Dictate anywhere": ("dictation",), "Pictures from words": ("apple_shortcuts",),
    "Translate in place": ("screen",), "Point at things on screen": ("screen",),
    "Teach me a task": ("input",), "Meeting notes": ("microphone",),
}


# --- status -------------------------------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_opener: list = [None]          # (page, anchor) -> None: opens Settings there (main.py sets it)


def _name() -> str:
    try:
        from mint.core import prefs
        return prefs.name() or "Mint"
    except Exception:
        return "Mint"


def _say(text: str) -> str:
    return (text or "").replace("{name}", _name())


def page_title(key: str) -> str:
    try:
        from mint.ui import settings as settings_window
        key = settings_window.page_for(key)
        return dict((k, t) for k, t, _ in settings_window.PAGES).get(key) or PAGE_TITLES.get(key, key)
    except Exception:
        return PAGE_TITLES.get(key, key)


def get(fid: str) -> Feature | None:
    return _BY_ID.get(fid)


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("-", " ").replace("_", " "))
    return " ".join(text.split())


def resolve(words: str) -> Feature | None:
    """The feature `words` mean ("connect my phone" -> telegram, "gmail" -> google): the longest name found in
    them wins; else the most words in common with a feature's names and title."""
    text = _norm(words)
    if not text:
        return None
    if text in _BY_ID or text.replace(" ", "_") in _BY_ID:
        return _BY_ID.get(text) or _BY_ID[text.replace(" ", "_")]
    padded = f" {text} "
    best, score = None, 0.0
    for feature in FEATURES:
        for name in feature.names + (_norm(feature.title), feature.id.replace("_", " ")):
            name = _norm(name)
            if name and f" {name} " in padded:
                value = len(name.split()) * 100 + len(name)
                if value > score:
                    best, score = feature, value
    if best is not None:
        return best
    wanted = {w for w in text.split() if len(w) > 2 and w not in {"the", "and", "set", "connect", "add", "how",
                                                                   "can", "you", "for", "with", "setup", "my"}}
    for feature in FEATURES:
        words_of = set(" ".join(_norm(n) for n in feature.names + (feature.title,)).split())
        value = len(wanted & words_of)
        if value > score:
            best, score = feature, value
    return best


def status(fid: str, fresh: bool = False) -> dict:
    """{id, title, state, detail, enables, page, anchor, steps}: is it set up? Cached TTL seconds; any thread."""
    feature = _BY_ID[fid]
    now = time.monotonic()
    with _lock:
        hit = _cache.get(fid)
    if hit is not None and not fresh and now - hit[0] < TTL:
        return dict(hit[1])
    try:
        found = feature.check() if feature.check else _r(UNKNOWN, "")
    except Exception as error:
        log.info("setup status %s: %s", fid, error)
        found = _r(UNKNOWN, f"Couldn't check ({str(error)[:60]})")
    result = {"id": fid, "title": feature.title, "state": found.get("state", UNKNOWN),
              "detail": _say(found.get("detail", "")), "enables": _say(feature.enables),
              "page": found.get("page") or feature.page,
              "anchor": found.get("anchor", feature.anchor if not found.get("page") else ""),
              "steps": _say(found.get("step") or feature.steps), "need": _say(found.get("need") or feature.need)}
    with _lock:
        _cache[fid] = (time.monotonic(), result)
    return dict(result)


def summary(fresh: bool = False) -> list[dict]:
    """Every feature's status, in the guide's order."""
    return [status(f.id, fresh) for f in FEATURES]


def refresh() -> None:
    with _lock:
        _cache.clear()


def where(result: dict) -> str:
    """'Settings ▸ Accounts & connections ▸ Telegram'."""
    parts = ["Settings", page_title(result["page"])]
    feature = _BY_ID.get(result["id"])
    if result.get("anchor") and feature is not None and result["anchor"] not in ("permissions",):
        parts.append(feature.title)
    elif result.get("anchor") == "permissions":
        parts.append("Permissions")
    return " ▸ ".join(parts)


def _done(result: dict) -> bool:
    return result["state"] == READY


# --- the "what can you do" card ------------------------------------------------------------------------------

def groups(statuses: dict | None = None) -> list[dict]:
    """The card's rows: {title, detail, trailing, icon} - each group Ready, or what it still needs."""
    statuses = statuses if statuses is not None else {s["id"]: s for s in summary()}
    rows = []
    for title, icon, what, needs, any_one in GROUPS:
        found = [statuses[n] for n in needs if n in statuses]
        open_ = [s for s in found if s["state"] in (MISSING, PARTIAL)]
        if any_one and any(_done(s) for s in found):
            open_ = []
        if any_one and open_:
            open_ = open_[:1]
        if open_:
            need = ", ".join(s["need"] for s in open_[:2])
            rows.append({"title": title, "detail": need[:1].upper() + need[1:], "trailing": "Set up", "icon": icon})
        else:
            rows.append({"title": title, "detail": what, "trailing": "Ready", "icon": icon})
    return rows


def _tour_lines(statuses: dict) -> tuple[list[str], list[str]]:
    """The tour's features (onboarding.TOUR): (ready titles, 'title (needs ...)')."""
    try:
        from mint.ui import onboarding
        titles = [item[2] for item in onboarding.TOUR]
    except Exception:
        titles = list(TOUR_NEEDS)
    ready, needs = [], []
    for title in titles:
        open_ = [statuses[n] for n in TOUR_NEEDS.get(title, ()) if n in statuses
                 and statuses[n]["state"] in (MISSING, PARTIAL)]
        if open_:
            needs.append(f"{title} (needs: {', '.join(s['need'] for s in open_)})")
        else:
            ready.append(title)
    return ready, needs


def show_card(statuses: dict | None = None) -> list[dict]:
    rows = groups(statuses)
    todo = sum(1 for r in rows if r["trailing"] != "Ready")
    from mint.tools import cards
    cards.show(f"What {_name()} can do", subtitle=(f"{todo} need a quick setup - say “set up …”" if todo
                                                   else "Everything is set up"),
               items=rows, icon="sparkles", tint="mint", seconds=30.0)
    return rows


# --- the tools -----------------------------------------------------------------------------------------------

def _line(result: dict) -> str:
    state = {READY: "set up", PARTIAL: "half set up", MISSING: "not set up", UNAVAILABLE: "not available here",
             UNKNOWN: "unknown"}[result["state"]]
    detail = f" ({result['detail']})" if result.get("detail") else ""
    return f"{result['title']}: {state}{detail}"


def setup_status(args: dict) -> str:
    args = args or {}
    words = str(args.get("feature") or "").strip()
    if words and words.lower() not in ("all", "everything"):
        feature = resolve(words)
        if feature is None:
            return f"No setup called '{words}'. Known: " + ", ".join(f.title for f in FEATURES) + "."
        result = status(feature.id)
        text = _line(result) + f". It enables: {result['enables']}"
        if not _done(result) and result["state"] != UNAVAILABLE:
            text += (f" To set it up: {where(result)} - {result['steps']} (open_setup opens it there, only if the "
                     "user wants to set it up now.)")
        return text + " Answer in one short sentence."
    statuses = {s["id"]: s for s in summary()}
    if _flag(args.get("tour")):
        try:
            from mint.ui import onboarding
            onboarding.show_tour()
            opened = "Opened the “What I can do” window (each feature with a short video). "
        except Exception as error:
            log.info("tour: %s", error)
            opened = ""
    else:
        opened = ""
    if _flag(args.get("card")) or opened:
        if _flag(args.get("card")):
            show_card(statuses)
        ready, needs = _tour_lines(statuses)
        missing = [s for s in statuses.values() if s["state"] in (MISSING, PARTIAL) and s["need"]
                   and s["id"] != "permissions"]
        return (opened + ("Shown as a card: what you can do, grouped, each marked Ready or what it needs. "
                          if _flag(args.get("card")) else "")
                + "Ready now: " + "; ".join(ready) + ". "
                + ("Needs setup first: " + "; ".join(needs) + ". " if needs else "")
                + ("Other things not set up: " + ", ".join(s["need"] for s in missing
                                                         if not any(s["need"] in n for n in needs))[:400] + ". "
                   if missing else "")
                + "Say in one or two short sentences what you can do (a few highlights - the card has the rest), "
                  "and at most one thing worth setting up, as an offer. Don't open Settings unless they say yes.")
    listed = [s for s in statuses.values() if s["id"] != "permissions"]
    perms = [s for s in listed if _BY_ID[s["id"]].page == "storage"]
    rest = [s for s in listed if s not in perms]
    ready = [s for s in rest if s["state"] == READY]
    todo = [s for s in rest if s["state"] in (MISSING, PARTIAL)]
    gone = [s for s in rest if s["state"] == UNAVAILABLE]
    allowed = [s["title"] for s in perms if s["state"] == READY]
    refused = [f"{s['title']} ({s['enables'].rstrip('.')})" for s in perms if s["state"] != READY]
    return ("Set up: " + "; ".join(s["title"] + _detail(s) for s in ready) + ".\n"
            + ("Not set up: " + "; ".join(f"{_line(s)} - would let you: {s['enables']}" for s in todo) + "\n"
               if todo else "")
            + ("Not on this Mac: " + "; ".join(_line(s) for s in gone) + ".\n" if gone else "")
            + "Permissions allowed: " + (", ".join(allowed) or "none") + ".\n"
            + ("Permissions NOT allowed: " + "; ".join(refused) + ".\n" if refused else "")
            + "Answer only what the user asked, briefly. To set one up: open_setup, only if they want to.")


def _detail(result: dict) -> str:
    return f" ({result['detail']})" if result.get("detail") and result["detail"] not in ("Allowed", "Key added") \
        else ""


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "1", "on")


def set_opener(opener: Callable | None) -> None:
    """main.py: opener(page, anchor) opens Settings at that page (and part of it)."""
    _opener[0] = opener


def _open(page: str, anchor: str) -> bool:
    opener = _opener[0]
    if opener is None:
        return False
    try:
        opener(page, anchor)
        return True
    except Exception:
        log.exception("could not open Settings at %s/%s", page, anchor)
        return False


def open_setup(args: dict) -> str:
    args = args or {}
    words = str(args.get("feature") or "").strip()
    reason = " ".join(str(args.get("reason") or "").split())
    if not words:
        return "NOT OPENED: say which feature (telegram, gmail, google meet, openai key, a permission...)."
    if not reason:
        return ("NOT OPENED: give `reason` - the user's own words asking to set this up or open its settings. "
                "Never open Settings on your own.")
    feature = resolve(words)
    if feature is None:
        return (f"NOT OPENED: no setup called '{words}'. Known: " + ", ".join(f.title for f in FEATURES)
                + ". To connect a particular app (Notion, Things...) use the connector tool instead.")
    result = status(feature.id, fresh=True)
    log.info("open_setup %s (%s): %s - %s", feature.id, reason[:80], result["state"], result["detail"])
    place = where(result)
    if result["state"] == UNAVAILABLE:
        return (f"NOT OPENED: {result['title']} - {result['detail']}. Tell the user in one short sentence; "
                "there is nothing to set up in Settings for it.")
    if _done(result) and not _flag(args.get("open_anyway")):
        return (f"ALREADY SET UP - nothing opened: {_line(result)}. Tell the user it's already set up, in one short "
                f"sentence (it lets them: {result['enables']}). Don't offer to open Settings; open_setup with "
                "open_anyway only if they then ask to see it.")
    if not _open(result["page"], result.get("anchor") or ""):
        return (f"NOT OPENED (Settings can't be opened from here). {_line(result)}. Tell the user where: {place}. "
                f"Steps: {result['steps']}")
    refresh()                   # what they do there shows on the next check
    if _done(result):
        return f"OPENED {place} as asked ({_line(result)}). Say so in a few words."
    return (f"OPENED {place}. {_line(result)}. Tell the user in at most two short sentences what to do there: "
            f"{result['steps']} Then stop; don't read out the rest of the page.")


HANDLERS = {"setup_status": setup_status, "open_setup": open_setup}

PROMPT = """Your own setup: "what can you do?" -> setup_status card=true, then one or two short sentences. "Connect / set \
up / how do I set up X" -> open_setup with reason=their words. NEVER open Settings on your own or to fix a \
failure: say what's missing and offer; open only after their yes. An app's connector (Notion, Things) is the \
connector tool; a shortcut that does something is make_shortcut."""


def declarations():
    from google.genai import types
    S, B = types.Type.STRING, types.Type.BOOLEAN
    return [
        types.FunctionDeclaration(
            name="setup_status",
            description=("What you can do right now and what is set up on this Mac: macOS permissions (microphone, "
                         "accessibility, screen recording, calendars...), keys (Gemini, TypeSafe, OpenAI, other "
                         "model providers), Google/Gmail via macOS accounts, Google Meet sign-in, Telegram, Email "
                         "control, Claude Code, Apple Shortcuts, voice training. For 'what can you do?' pass "
                         "card=true: your features, grouped, shown as a card, each marked Ready or what it needs. "
                         "For 'is Telegram connected?' pass feature. Read-only: opens nothing (tour=true only when "
                         "the user asks for the tour or the videos)."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "feature": types.Schema(type=S, description="One thing, in the user's words ('telegram', 'gmail', "
                                                            "'calendar access', 'openai key'); empty = all."),
                "card": types.Schema(type=B, description="true for 'what can you do?': show the features card."),
                "tour": types.Schema(type=B, description="true only when the user asks for the tour or videos of "
                                                         "what you can do: opens that window."),
            })),
        types.FunctionDeclaration(
            name="open_setup",
            description=("Help the user set up or connect one of your features: Telegram, Gmail/Google account, "
                         "Google Meet, Claude Code, Apple Shortcuts, Email control, a Gemini / TypeSafe / OpenAI / "
                         "other model key, voice training, or a macOS permission. ONLY when the user asks to set "
                         "up, connect, add or 'how do I' it, or to open its settings - never on your own. Already "
                         "set up: nothing opens, just tell them. Otherwise Settings opens at the right place and "
                         "you get the steps: say them in at most two short sentences."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "feature": types.Schema(type=S, description="What to set up, in the user's words: 'telegram', "
                                                            "'gmail', 'google meet', 'openai key', 'calendar'."),
                "reason": types.Schema(type=S, description="The user's own words asking for it."),
                "open_anyway": types.Schema(type=B, description="true only if the user explicitly asked to open "
                                                                "or see its settings although it is set up."),
            }, required=["feature", "reason"])),
    ]
