"""Apple Shortcuts from plain words: "make me a shortcut that says hello and shows today's date",
"a shortcut that sets the volume to 30% and turns on dark mode".

    plan    Gemini turns the description into steps from a vetted catalog of Shortcuts actions
            and Mint reads them back in plain words (or says what Shortcuts can't do here)
    create  after the user agrees: the steps become a shortcut plist, `shortcuts sign --mode
            anyone` signs it and Mint opens it; the user clicks "Add Shortcut" once (a Mac app
            can't add one itself). A background check of `shortcuts list` (up to 5 minutes)
            then says "Added '<name>' - say 'run <name>'".

The catalog only holds actions whose plist format was copied from real shortcuts: Apple's own
Gallery workflows (WorkflowKit.framework/Resources/Gallery.bundle), public iCloud shortcuts
(https://www.icloud.com/shortcuts/api/records/<id>, downloaded as shortcut.plist) and Mint's
own shortcuts that work live (imagegen.py, macctl.py). Parameter keys and choices were also
checked against WorkflowKit's action definitions on this Mac. Each entry names its sources.

Values in steps may point at earlier results: {prev} (the result just before), {step N} (the
result of step N), {date} (the current date), {input} (what the shortcut is given), {repeat
index} (inside a repeat). They become Shortcuts' own variable tokens.

Never here: sending messages or email (email is a draft the user sends), deleting anything,
running shell commands without saying so in the plan, anything run as administrator.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import uuid

log = logging.getLogger("mint.tools.shortcut_maker")

MODELS = ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
POLL_SECONDS = 300
_state: dict = {"plans": {}, "last": None, "status": {}, "listeners": []}
_lock = threading.Lock()


class PlanError(ValueError):
    """A step that can't be built (a bad value, a missing app, a result that doesn't exist)."""


# --- Tokens (the plist format of imagegen.py's working shortcuts) ------------------------------

def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _attachment(value: dict) -> dict:
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


_TOKEN = re.compile(r"\{\s*(prev|previous|result|date|now|today|input|repeat[ _]index|step\s*(\d+))\s*\}", re.I)


class _Builder:
    """Turns catalog steps into WFWorkflowActions, keeping track of which results exist."""

    def __init__(self) -> None:
        self.actions: list[dict] = []
        self.last: tuple[str, str] | None = None          # (UUID, output name) of the latest result
        self.steps: dict[int, tuple[str, str]] = {}        # top-level step number -> its result
        self.said = None                                   # the last text shown/spoken: the shortcut's output
        self.uses_input = False
        self.repeat_depth = 0

    def add(self, identifier: str, params: dict, out: str | None = None) -> str:
        made = _uuid()                     # only actions with a result carry a UUID, as in real shortcuts
        self.actions.append({"WFWorkflowActionIdentifier": identifier,
                             "WFWorkflowActionParameters": {"UUID": made, **params} if out else dict(params)})
        if out:
            self.last = (made, out)
        return made

    def _ref(self, match: re.Match) -> dict:
        word = match.group(1).lower().replace(" ", "_")
        if word in ("prev", "previous", "result"):
            if self.last is None:
                raise PlanError("{prev} is used before any step gives a result")
            return {"Type": "ActionOutput", "OutputName": self.last[1], "OutputUUID": self.last[0]}
        if word in ("date", "now", "today"):
            return {"Type": "CurrentDate"}
        if word == "input":
            self.uses_input = True
            return {"Type": "ExtensionInput"}
        if word == "repeat_index":
            if not self.repeat_depth:
                raise PlanError("{repeat index} is used outside a repeat")
            return {"Type": "Variable", "VariableName": "Repeat Index"}
        number = int(match.group(2))
        if number not in self.steps:
            raise PlanError(f"step {number} has no result to use")
        made, name = self.steps[number]
        return {"Type": "ActionOutput", "OutputName": name, "OutputUUID": made}

    def text(self, value) -> str | dict:
        """A text parameter: plain, or a WFTextTokenString with the results it mentions."""
        value = "" if value is None else str(value)
        if not _TOKEN.search(value):
            return value
        parts, ranges, at = [], {}, 0
        for match in _TOKEN.finditer(value):
            parts.append(value[at:match.start()])
            ranges[f"{{{_utf16(''.join(parts))}, 1}}"] = self._ref(match)
            parts.append("￼")
            at = match.end()
        parts.append(value[at:])
        return {"Value": {"attachmentsByRange": ranges, "string": "".join(parts)},
                "WFSerializationType": "WFTextTokenString"}

    def say(self, value) -> str | dict:
        """text() for what the shortcut shows or speaks; the last one is also its output."""
        made = self.text(value)
        self.said = json.loads(json.dumps(made))
        return made

    def content(self, value) -> dict:
        """A parameter that takes one variable (Copy to Clipboard, Quick Look, Play Music): a result
        is passed straight in; written text first goes through a Text action."""
        value = "{prev}" if value in (None, "") else str(value)
        match = _TOKEN.fullmatch(value.strip())
        if match:
            return _attachment(self._ref(match))
        made = self.add("is.workflow.actions.gettext", {"WFTextActionText": self.text(value)}, "Text")
        return _attachment({"Type": "ActionOutput", "OutputName": "Text", "OutputUUID": made})


# --- Values ---------------------------------------------------------------------------------

def _percent(value) -> float:
    try:
        number = float(str(value).strip().rstrip("%"))
    except ValueError:
        raise PlanError(f"'{value}' is not a percentage") from None
    if 0 < number <= 1 and "." in str(value):          # 0.3 means 30%
        number *= 100
    return round(max(0.0, min(100.0, number)) / 100, 4)


def _number(value, low: float = 0, high: float = 10**6) -> float:
    try:
        number = float(str(value).strip())
    except ValueError:
        raise PlanError(f"'{value}' is not a number") from None
    return max(low, min(high, number))


def _state_value(value) -> str:
    word = str(value or "on").strip().lower()
    if word in ("on", "true", "1", "yes", "enable", "enabled"):
        return "on"
    if word in ("off", "false", "0", "no", "disable", "disabled"):
        return "off"
    if word in ("toggle", "switch"):
        return "toggle"
    raise PlanError(f"'{value}' should be on, off or toggle")


def _choice(value, choices: dict[str, str], default: str) -> str:
    word = str(value or default).strip().lower()
    for key, real in choices.items():
        if word == key or word == real.lower():
            return real
    raise PlanError(f"'{value}' should be one of: {', '.join(choices)}")


def _is_ref(value) -> bool:
    return bool(_TOKEN.fullmatch(str(value or "").strip()))


# --- The catalog ----------------------------------------------------------------------------
# Each entry: the action identifier(s), what it does, its values (name: (kind, about)),
# the name of the result it passes on (None = nothing / it passes its input through), a flag
# for what the user must be told, where its format was checked, and build/say functions.

A = "is.workflow.actions."


def _b_toggle(identifier: str):
    def build(v, b):
        state = _state_value(v.get("state"))
        b.add(A + identifier, {"operation": "toggle"} if state == "toggle"
              else {"operation": "set", "OnValue": 1 if state == "on" else 0})
    return build


def _say_toggle(what: str):
    def say(v):
        state = _state_value(v.get("state"))
        return f"Switch {what} {'on or off' if state == 'toggle' else state}" if state == "toggle" \
            else f"Turn {what} {state}"
    return say


FOCUS = {"do not disturb": ("com.apple.donotdisturb.mode.default", "Do Not Disturb"),
         "work": ("com.apple.focus.work", "Work"), "personal": ("com.apple.focus.personal-time", "Personal"),
         "reading": ("com.apple.focus.reading", "Reading")}


def _b_focus(v, b):
    state = _state_value(v.get("state"))
    mode = str(v.get("mode") or "do not disturb").strip().lower().replace("dnd", "do not disturb")
    if mode not in FOCUS:
        raise PlanError(f"Focus '{mode}' is not one Mint can set (only {', '.join(FOCUS)})")
    ident, shown = FOCUS[mode]
    params = {"FocusModes": {"Identifier": ident, "DisplayString": shown}}
    if state == "toggle":
        params.update(Operation="Toggle")
    else:
        params.update(Operation="Turn", Enabled=1 if state == "on" else 0)
        if state == "on":
            params["AssertionType"] = "Turned Off"
    b.add(A + "dnd.set", params)


def _app(name: str) -> dict:
    """{BundleIdentifier, Name, TeamIdentifier} of an installed app, as Open App stores it."""
    import AppKit
    from mint.tools import appfinder
    real, _how = appfinder.resolve(name)
    path = appfinder.installed().get(real or "")
    bundle = AppKit.NSBundle.bundleWithPath_(path) if path else None
    ident = str(bundle.bundleIdentifier() or "") if bundle is not None else ""
    if not ident:
        raise PlanError(f"there is no app called '{name}' on this Mac")
    team = "0000000000"                      # what Shortcuts writes for Apple's own apps
    try:
        signed = subprocess.run(["codesign", "-dv", path], capture_output=True, text=True, timeout=10)
        found = re.search(r"TeamIdentifier=([A-Z0-9]{10})", signed.stderr)
        if found:
            team = found.group(1)
    except Exception:
        pass
    return {"BundleIdentifier": ident, "Name": real, "TeamIdentifier": team}


def _b_open_app(v, b):
    app = _app(str(v.get("app") or ""))
    b.add(A + "openapp", {"WFAppIdentifier": app["BundleIdentifier"], "WFSelectedApp": app}, "App")


def _b_open_url(v, b):
    url = str(v.get("url") or "").strip()
    if not url:
        raise PlanError("open_url needs a web address")
    if not _is_ref(url) and "://" not in url and ":" not in url.split("/")[0]:
        url = "https://" + url
    made = b.add(A + "url", {"WFURLActionURL": b.text(url)}, "URL")
    b.add(A + "openurl", {"WFInput": _attachment({"Type": "ActionOutput", "OutputName": "URL", "OutputUUID": made})})


def _b_email(v, b):
    to = [a.strip() for a in re.split(r"[,;\s]+", str(v.get("to") or "")) if a.strip()]
    bad = [a for a in to if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a)]
    if bad:
        raise PlanError(f"'{bad[0]}' is not an email address")
    params = {"WFSendEmailActionShowComposeSheet": True,          # a draft: the user presses Send
              "WFSendEmailActionSubject": b.text(v.get("subject") or ""),
              "WFSendEmailActionInputAttachments": b.text(v.get("body") or "")}
    if to:
        params["WFSendEmailActionToRecipients"] = {
            "Value": {"WFContactFieldValues": [{"EntryType": 2, "SerializedEntry": {"link.contentkit.emailaddress": a}}
                                               for a in to]},
            "WFSerializationType": "WFContactFieldValue"}
    b.add(A + "sendemail", params)


# Shell commands that are never put in a shortcut, whatever the plan says.
_SHELL_NEVER = re.compile(r"\b(sudo|rm|rmdir|srm|shred|mkfs|newfs|dd|diskutil|fdisk|launchctl|csrutil|chmod|chown|"
                          r"kill|killall|pkill|shutdown|reboot|halt|passwd|dscl|security|defaults\s+delete|"
                          r"tmutil\s+delete|osascript)\b|>\s*/|curl[^|]*\|\s*(ba|z)?sh|:\(\)\s*\{", re.I)


def _b_shell(v, b):
    script = str(v.get("script") or "").strip()
    if not script:
        raise PlanError("run_shell needs a command")
    if _SHELL_NEVER.search(script):
        raise PlanError("that shell command could delete or change system things; Mint won't put it in a shortcut")
    b.add(A + "runshellscript", {"Script": b.text(script), "Shell": "/bin/zsh", "InputMode": "to stdin"},
          "Shell Script Result")


def _b_image(v, b):
    from mint.tools import imagegen
    prompt = str(v.get("prompt") or "").strip()
    if not prompt:
        raise PlanError("create_image needs a description")
    # No style: a fixed style entity was rejected on import ("Please choose a value for each parameter").
    b.add(imagegen._ACTION, {"AppIntentDescriptor": dict(imagegen._DESCRIPTOR), "prompt": b.text(prompt),
                             "saveToLibrary": "always"}, "Image")


def _b_date(v, b):
    styles = {"short": "Short", "medium": "Medium", "long": "Long", "full": "Full"}
    show = str(v.get("show") or "date").strip().lower()
    style = _choice(v.get("style"), styles, "long")
    params = {"WFDate": b.text(v.get("date") or "{date}")}
    if show == "time":
        params.update(WFDateFormatStyle="None", WFTimeFormatStyle="Short")
    elif show in ("both", "date and time"):
        params.update(WFDateFormatStyle=style, WFTimeFormatStyle="Short")
    else:
        params.update(WFDateFormatStyle=style, WFTimeFormatStyle="None")
    b.add(A + "format.date", params, "Formatted Date")


def _b_event(v, b):
    title = str(v.get("title") or "").strip()
    start = str(v.get("start") or "").strip()
    if not title or not start:
        raise PlanError("add_event needs a title and a start time")
    params = {"WFCalendarItemTitle": b.text(title), "WFCalendarItemStartDate": b.text(start),
              "WFCalendarItemDates": True, "ShowWhenRun": False}
    if v.get("end"):
        params["WFCalendarItemEndDate"] = b.text(v["end"])
    if str(v.get("all_day") or "").lower() in ("true", "yes", "1"):
        params["WFCalendarItemAllDay"] = True
    if v.get("location"):
        params["WFCalendarItemLocation"] = b.text(v["location"])
    if v.get("notes"):
        params["WFCalendarItemNotes"] = b.text(v["notes"])
    if v.get("calendar"):
        params["WFCalendarItemCalendar"] = str(v["calendar"])
        params["WFCalendarDescriptor"] = {"Title": str(v["calendar"]), "IsAllCalendar": False}
    b.add(A + "addnewevent", params, "New Event")


def _b_reminder(v, b):
    title = str(v.get("title") or "").strip()
    if not title:
        raise PlanError("add_reminder needs a title")
    params = {"WFCalendarItemTitle": b.text(title)}
    if v.get("list"):
        params["WFCalendarItemCalendar"] = str(v["list"])
        params["WFCalendarDescriptor"] = {"Title": str(v["list"]), "IsAllCalendar": False}
    if v.get("notes"):
        params["WFCalendarItemNotes"] = b.text(v["notes"])
    if v.get("remind_at"):
        params.update(WFAlertEnabled="Alert", WFAlertCondition="At Time", WFAlertCustomTime=b.text(v["remind_at"]))
    if v.get("priority"):
        params["WFPriority"] = _choice(v["priority"], {"none": "None", "low": "Low", "medium": "Medium",
                                                       "high": "High"}, "none")
    b.add(A + "addnewreminder", params, "New Reminder")


def _b_playlist(v, b):
    name = str(v.get("playlist") or "").strip()
    if not name:
        raise PlanError("play_playlist needs a playlist name")
    made = b.add(A + "get.playlist", {"WFPlaylistName": name}, "Playlist")
    shuffle = str(v.get("shuffle") or "").lower() in ("true", "yes", "1", "on")
    b.add(A + "playmusic", {"WFMediaItems": _attachment({"Type": "ActionOutput", "OutputName": "Playlist",
                                                         "OutputUUID": made}),
                            "WFPlayMusicActionShuffle": "Songs" if shuffle else "Off"})


def _b_menu(v, b):
    options = [o for o in (v.get("options") or []) if isinstance(o, dict) and str(o.get("title") or "").strip()]
    if len(options) < 2:
        raise PlanError("a menu needs at least two choices")
    group = _uuid()
    b.actions.append({"WFWorkflowActionIdentifier": A + "choosefrommenu", "WFWorkflowActionParameters": {
        "WFMenuPrompt": str(v.get("prompt") or "Choose"), "WFControlFlowMode": 0, "GroupingIdentifier": group,
        "WFMenuItems": [str(o["title"]).strip() for o in options]}})
    for option in options:
        b.actions.append({"WFWorkflowActionIdentifier": A + "choosefrommenu", "WFWorkflowActionParameters": {
            "WFMenuItemTitle": str(option["title"]).strip(), "WFControlFlowMode": 1, "GroupingIdentifier": group}})
        _build_steps(option.get("steps") or [], b, top=False)
    end = _uuid()
    b.actions.append({"WFWorkflowActionIdentifier": A + "choosefrommenu", "WFWorkflowActionParameters": {
        "UUID": end, "WFControlFlowMode": 2, "GroupingIdentifier": group}})
    b.last = (end, "Menu Result")


def _b_repeat(v, b):
    times = int(_number(v.get("times") or 1, 1, 100))
    group = _uuid()
    b.actions.append({"WFWorkflowActionIdentifier": A + "repeat.count", "WFWorkflowActionParameters": {
        "WFRepeatCount": float(times), "WFControlFlowMode": 0, "GroupingIdentifier": group}})
    b.repeat_depth += 1
    _build_steps(v.get("steps") or [], b, top=False)
    b.repeat_depth -= 1
    end = _uuid()
    b.actions.append({"WFWorkflowActionIdentifier": A + "repeat.count", "WFWorkflowActionParameters": {
        "UUID": end, "WFControlFlowMode": 2, "GroupingIdentifier": group}})
    b.last = (end, "Repeat Results")


def _words(value) -> str:
    """A value with its result tokens in plain words, for reading the plan back."""
    def name(match):
        word = match.group(1).lower()
        if word in ("prev", "previous", "result"):
            return "[the result before]"
        if word in ("date", "now", "today"):
            return "[today's date]"
        if word == "input":
            return "[what the shortcut is given]"
        if word.startswith("repeat"):
            return "[the repeat number]"
        return f"[the result of step {match.group(2)}]"
    return _TOKEN.sub(name, str(value or "")).strip()


def _q(value, n: int = 80) -> str:
    text = _words(value)
    return f"“{text[:n]}{'…' if len(text) > n else ''}”"


CATALOG: dict[str, dict] = {
    # Text and showing
    "text": {"ids": [A + "gettext"], "does": "a piece of text (can include earlier results)",
             "values": {"text": "the text"}, "out": "Text", "source": "Apple Gallery; iCloud d10cc0f4, 3a98d96d",
             "build": lambda v, b: b.add(A + "gettext", {"WFTextActionText": b.text(v.get("text"))}, "Text"),
             "say": lambda v: f"Make the text {_q(v.get('text'))}"},
    "ask": {"ids": [A + "ask"], "does": "ask the user to type something when it runs",
            "values": {"prompt": "the question", "type": "text or number", "default": "optional default answer"},
            "out": "Provided Input", "source": "Apple Gallery Haiku/ActionItems; iCloud 3aee50ac",
            "build": lambda v, b: b.add(A + "ask", {k: x for k, x in {
                "WFAskActionPrompt": str(v.get("prompt") or "").strip(),
                "WFInputType": "Number" if str(v.get("type") or "").lower().startswith("num") else "Text",
                "WFAskActionDefaultAnswer": str(v.get("default") or "")}.items() if x != ""}, "Provided Input"),
            "say": lambda v: f"Ask you: {_q(v.get('prompt'))}"},
    "show": {"ids": [A + "showresult"], "does": "show text in a window (Show Result)",
             "values": {"text": "what to show"}, "out": None, "source": "Apple Gallery DocumentReview/Haiku",
             "build": lambda v, b: b.add(A + "showresult", {"Text": b.say(v.get("text") or "{prev}")}),
             "say": lambda v: f"Show {_q(v.get('text') or '{prev}')}"},
    "alert": {"ids": [A + "alert"], "does": "an alert with OK (waits for a click)",
              "values": {"title": "optional title", "message": "the message"}, "out": None,
              "source": "iCloud b7ccf733, 3aee50ac",
              "build": lambda v, b: b.add(A + "alert", {"WFAlertActionTitle": b.text(v.get("title") or ""),
                                                        "WFAlertActionMessage": b.text(v.get("message") or "{prev}"),
                                                        "WFAlertActionCancelButtonShown": False}),
              "say": lambda v: f"Show an alert {_q(v.get('message') or '{prev}')}"},
    "speak": {"ids": [A + "speaktext"], "does": "say text out loud",
              "values": {"text": "what to say"}, "out": None, "source": "iCloud 3a98d96d, 03c9d317, 822cbf0b",
              "build": lambda v, b: b.add(A + "speaktext", {"WFText": b.say(v.get("text") or "{prev}")}),
              "say": lambda v: f"Say {_q(v.get('text') or '{prev}')} out loud"},
    "notify": {"ids": [A + "notification"], "does": "a notification banner",
               "values": {"title": "optional title", "body": "the text", "sound": "true/false"}, "out": None,
               "source": "iCloud 3a98d96d, 4865adf1",
               "build": lambda v, b: b.add(A + "notification", {
                   "WFNotificationActionTitle": b.text(v.get("title") or ""),
                   "WFNotificationActionBody": b.text(v.get("body") or "{prev}"),
                   "WFNotificationActionSound": str(v.get("sound", "true")).lower() not in ("false", "no", "0")}),
               "say": lambda v: f"Show a notification {_q(v.get('body') or '{prev}')}"},
    # Clipboard, apps, web
    "get_clipboard": {"ids": [A + "getclipboard"], "does": "what is on the clipboard", "values": {},
                      "out": "Clipboard", "source": "iCloud 221808be, b7446d12",
                      "build": lambda v, b: b.add(A + "getclipboard", {}, "Clipboard"),
                      "say": lambda v: "Get what's on the clipboard"},
    "copy": {"ids": [A + "setclipboard"], "does": "copy text or a result to the clipboard",
             "values": {"content": "text, or {prev} / {step N}"}, "out": None, "source": "iCloud 16716693, e066c42a",
             "build": lambda v, b: b.add(A + "setclipboard", {"WFInput": b.content(v.get("content"))}),
             "say": lambda v: f"Copy {_q(v.get('content') or '{prev}')} to the clipboard"},
    "open_app": {"ids": [A + "openapp"], "does": "open an app on this Mac", "values": {"app": "the app's name"},
                 "out": None, "source": "iCloud 33c85237, 365ace95 (Mac and iOS apps)", "build": _b_open_app,
                 "say": lambda v: f"Open {str(v.get('app') or '').strip()}"},
    "open_url": {"ids": [A + "url", A + "openurl"], "does": "open a web address (or app link) in the browser",
                 "values": {"url": "the address"}, "out": None, "source": "iCloud 16716693, ba81c407, 76535cac",
                 "build": _b_open_url, "say": lambda v: f"Open {_words(v.get('url'))}"},
    "search_web": {"ids": [A + "searchweb"], "does": "search the web",
                   "values": {"text": "what to search for", "engine": "Google, DuckDuckGo, Bing, YouTube…"},
                   "out": None, "source": "iCloud 0cb54e35, 7b76ab85; choices from WorkflowKit",
                   "build": lambda v, b: b.add(A + "searchweb", {
                       "WFSearchWebDestination": _choice(v.get("engine"), {k.lower(): k for k in (
                           "Amazon", "Bing", "DuckDuckGo", "eBay", "Google", "Reddit", "YouTube")}, "google"),
                       "WFInputText": b.text(v.get("text") or "{prev}")}),
                   "say": lambda v: f"Search {v.get('engine') or 'Google'} for {_q(v.get('text') or '{prev}')}"},
    "wait": {"ids": [A + "delay"], "does": "wait some seconds", "values": {"seconds": "a number"}, "out": None,
             "source": "iCloud d10cc0f4, 3a98d96d",
             "build": lambda v, b: b.add(A + "delay", {"WFDelayTime": _number(v.get("seconds") or 1, 0, 3600)}),
             "say": lambda v: f"Wait {_number(v.get('seconds') or 1, 0, 3600):g} seconds"},
    # Mac settings
    "set_volume": {"ids": [A + "setvolume"], "does": "set the sound volume", "values": {"percent": "0-100"},
                   "out": None, "source": "iCloud 06feccc6, 8089edc7; key from WorkflowKit",
                   "build": lambda v, b: b.add(A + "setvolume", {"WFVolume": _percent(v.get("percent"))}),
                   "say": lambda v: f"Set the volume to {round(_percent(v.get('percent')) * 100)}%"},
    "set_brightness": {"ids": [A + "setbrightness"], "does": "set the screen brightness",
                       "values": {"percent": "0-100"}, "out": None, "source": "iCloud 216b2491, 4b5cadfc, ab78cbd8",
                       "build": lambda v, b: b.add(A + "setbrightness", {"WFBrightness": _percent(v.get("percent"))}),
                       "say": lambda v: f"Set the brightness to {round(_percent(v.get('percent')) * 100)}%"},
    "appearance": {"ids": [A + "appearance"], "does": "dark mode, light mode, or switch between them",
                   "values": {"mode": "dark, light or toggle"}, "out": None,
                   "source": "iCloud ab78cbd8, fe39eacb, 216b2491",
                   "build": lambda v, b: b.add(A + "appearance", {"operation": "toggle"} if str(
                       v.get("mode") or "").lower() == "toggle" else {"operation": "set", "style": _choice(
                           v.get("mode"), {"dark": "dark", "light": "light"}, "dark")}),
                   "say": lambda v: "Switch between dark and light mode" if str(v.get("mode") or "").lower() == "toggle"
                   else f"Turn on {str(v.get('mode') or 'dark').lower()} mode"},
    "focus": {"ids": [A + "dnd.set"], "does": "turn a Focus on or off",
              "values": {"mode": ", ".join(FOCUS), "state": "on, off or toggle"}, "out": None,
              "source": "Mint Focus On/Off (macctl.py, live); iCloud 7cc4fe46, deb96467, 9b4d0e39",
              "build": _b_focus,
              "say": lambda v: f"Turn {FOCUS.get(str(v.get('mode') or 'do not disturb').lower(), ('', str(v.get('mode'))))[1]}"
                               f" {_state_value(v.get('state'))}"},
    "wifi": {"ids": [A + "wifi.set"], "does": "Wi-Fi on/off", "values": {"state": "on, off or toggle"},
             "out": None, "source": "iCloud bc2f5240, c6d3d529, 103766d7", "build": _b_toggle("wifi.set"),
             "say": _say_toggle("Wi-Fi")},
    "bluetooth": {"ids": [A + "bluetooth.set"], "does": "Bluetooth on/off", "values": {"state": "on, off or toggle"},
                  "out": None, "source": "iCloud 3a98d96d, eabb56d9, 291013ee", "build": _b_toggle("bluetooth.set"),
                  "say": _say_toggle("Bluetooth")},
    "night_shift": {"ids": [A + "nightshift.set"], "does": "Night Shift on/off",
                    "values": {"state": "on, off or toggle"}, "out": None, "source": "iCloud 216b2491, caac0358",
                    "build": _b_toggle("nightshift.set"), "say": _say_toggle("Night Shift")},
    "low_power": {"ids": [A + "lowpowermode.set"], "does": "Low Power Mode on/off",
                  "values": {"state": "on, off or toggle"}, "out": None, "source": "iCloud 0cb54e35, 1b752db1",
                  "build": _b_toggle("lowpowermode.set"), "say": _say_toggle("Low Power Mode")},
    "screen_saver": {"ids": [A + "startscreensaver"], "does": "start the screen saver", "values": {}, "out": None,
                     "source": "iCloud cbf25d04, f0635f98",
                     "build": lambda v, b: b.add(A + "startscreensaver", {}), "say": lambda v: "Start the screen saver"},
    # Information
    "weather": {"ids": [A + "weather.currentconditions"], "does": "the current weather here", "values": {},
                "out": "Weather Conditions", "source": "Apple Gallery MorningReport/GetStartedWithModels",
                "build": lambda v, b: b.add(A + "weather.currentconditions", {}, "Weather Conditions"),
                "say": lambda v: "Get the current weather"},
    "battery": {"ids": [A + "getbatterylevel"], "does": "the battery level (a number)", "values": {},
                "out": "Battery Level", "source": "iCloud a25f319c, 82877008",
                "build": lambda v, b: b.add(A + "getbatterylevel", {}, "Battery Level"),
                "say": lambda v: "Get the battery level"},
    "date_text": {"ids": [A + "format.date"], "does": "today's date and/or time as text",
                  "values": {"show": "date, time or both", "style": "short, medium, long or full",
                             "date": "optional: another date as text; default now"},
                  "out": "Formatted Date", "source": "iCloud ca35aade, 7e63fe9d (Current Date token)",
                  "build": _b_date, "say": lambda v: "Get " + {"time": "the current time", "both": "the date and time",
                                                              "date and time": "the date and time"}.get(
                      str(v.get("show") or "date").lower(), "today's date") + " as text"},
    "upcoming_events": {"ids": [A + "getupcomingevents"], "does": "the next calendar events",
                        "values": {"count": "how many", "day": "today, tomorrow or any"},
                        "out": "Upcoming Events", "source": "iCloud 3067b42e, 37b1a92b",
                        "build": lambda v, b: b.add(A + "getupcomingevents", {
                            "WFGetUpcomingItemCount": _number(v.get("count") or 3, 1, 50),
                            "WFDateSpecifier": _choice(v.get("day"), {"today": "Today", "tomorrow": "Tomorrow",
                                                                      "any": "Any Day"}, "today"),
                            "WFGetUpcomingItemCalendar": {"IsAllCalendar": True}}, "Upcoming Events"),
                        "say": lambda v: f"Get the next {int(_number(v.get('count') or 3, 1, 50))} calendar events "
                                         f"({str(v.get('day') or 'today').lower()})"},
    "dictate": {"ids": [A + "dictatetext"], "does": "listen and turn speech into text", "values": {},
                "out": "Dictated Text", "source": "iCloud 7359b039, 61590f9e",
                "build": lambda v, b: b.add(A + "dictatetext", {}, "Dictated Text"),
                "say": lambda v: "Listen and write down what you say"},
    "random_number": {"ids": [A + "number.random"], "does": "a random whole number",
                      "values": {"min": "lowest", "max": "highest"}, "out": "Random Number",
                      "source": "iCloud d5127125",
                      "build": lambda v, b: b.add(A + "number.random", {
                          "WFRandomNumberMinimum": str(int(_number(v.get("min") or 1, -10**9, 10**9))),
                          "WFRandomNumberMaximum": str(int(_number(v.get("max") or 100, -10**9, 10**9)))},
                          "Random Number"),
                      "say": lambda v: f"Pick a random number from {v.get('min') or 1} to {v.get('max') or 100}"},
    "calculate": {"ids": [A + "calculateexpression"], "does": "work out a sum like 7 * (3 + 2)",
                  "values": {"expression": "the sum; may include results"}, "out": "Calculation Result",
                  "source": "iCloud 3a98d96d, 8274374d",
                  "build": lambda v, b: b.add(A + "calculateexpression", {"Input": b.text(v.get("expression"))},
                                              "Calculation Result"),
                  "say": lambda v: f"Work out {_q(v.get('expression'))}"},
    "ask_ai": {"ids": [A + "askllm"], "does": "ask Apple Intelligence (Use Model) and get its answer",
               "values": {"prompt": "the request; may include results"}, "out": "Response",
               "source": "Apple Gallery MorningReport/Haiku/DocumentReview",
               "build": lambda v, b: b.add(A + "askllm", {"WFLLMPrompt": b.text(v.get("prompt"))}, "Response"),
               "say": lambda v: f"Ask Apple Intelligence {_q(v.get('prompt'))}"},
    # Music, calendar, reminders, mail
    "play_playlist": {"ids": [A + "get.playlist", A + "playmusic"], "does": "play one of the user's Music playlists",
                      "values": {"playlist": "its name", "shuffle": "true/false"}, "out": None,
                      "source": "iCloud b84dbf06 (Get Playlist + Play Music), d1a60b6e",
                      "build": _b_playlist, "say": lambda v: f"Play the playlist {_q(v.get('playlist'))}"
                      + (" on shuffle" if str(v.get("shuffle") or "").lower() in ("true", "yes", "1", "on") else "")},
    "play_pause": {"ids": [A + "pausemusic"], "does": "play, pause, or play/pause music",
                   "values": {"do": "play, pause or toggle"}, "out": None, "source": "iCloud d961e80e, 6060dd1f",
                   "build": lambda v, b: b.add(A + "pausemusic", {"WFPlayPauseBehavior": _choice(
                       v.get("do"), {"play": "Play", "pause": "Pause", "toggle": "Play/Pause"}, "toggle")}),
                   "say": lambda v: {"play": "Play music", "pause": "Pause music"}.get(
                       str(v.get("do") or "").lower(), "Play or pause music")},
    "add_event": {"ids": [A + "addnewevent"], "does": "add a calendar event",
                  "values": {"title": "", "start": "e.g. 'tomorrow at 3 PM'", "end": "optional", "location": "optional",
                             "notes": "optional", "all_day": "true/false", "calendar": "optional calendar name"},
                  "out": "New Event", "source": "iCloud 9e1828a2, c2d0e2c9, 1b652d17", "build": _b_event,
                  "say": lambda v: f"Add the event {_q(v.get('title'))} at {_words(v.get('start'))}"},
    "add_reminder": {"ids": [A + "addnewreminder"], "does": "add a reminder",
                     "values": {"title": "", "list": "optional list", "remind_at": "optional time, e.g. 'today at 5 PM'",
                                "notes": "optional", "priority": "none, low, medium or high"},
                     "out": "New Reminder", "source": "Apple Gallery ActionItems; iCloud 9250ae6d, 754f8df7",
                     "build": _b_reminder,
                     "say": lambda v: f"Add the reminder {_q(v.get('title'))}"
                     + (f" (alert {_words(v.get('remind_at'))})" if v.get("remind_at") else "")},
    "email_draft": {"ids": [A + "sendemail"], "does": "open an email DRAFT in Mail (the user presses Send)",
                    "values": {"to": "addresses", "subject": "", "body": ""}, "out": None, "flag": "email",
                    "source": "iCloud 47d3a20c, 3d101812, 6e484533 (recipient format)", "build": _b_email,
                    "say": lambda v: f"Open an email draft to {v.get('to') or '(no one yet)'}: {_q(v.get('subject'), 50)}"},
    # Pictures
    "create_image": {"ids": ["com.apple.GenerativePlaygroundApp.GenerateImageIntent"],
                     "does": "make a picture with Image Playground (Apple Intelligence); saved in its library",
                     "values": {"prompt": "what the picture shows"}, "out": "Image",
                     "source": "Mint Draw (imagegen.py, live); iCloud 9abf18fe", "build": _b_image,
                     "say": lambda v: f"Make a picture of {_q(v.get('prompt'))} with Image Playground"},
    "quick_look": {"ids": [A + "previewdocument"], "does": "show a result (a picture, a file) in Quick Look",
                   "values": {"content": "{prev} or {step N}"}, "out": None, "source": "iCloud 171284b4, 750cf1db",
                   "build": lambda v, b: b.add(A + "previewdocument", {"WFInput": b.content(v.get("content"))}),
                   "say": lambda v: f"Show {_q(v.get('content') or '{prev}')} in Quick Look"},
    # Scripts and flow
    "run_shell": {"ids": [A + "runshellscript"], "does": "run a zsh command (only if the user clearly asked)",
                  "values": {"script": "the command"}, "out": "Shell Script Result", "flag": "shell",
                  "source": "iCloud 5b25691b, 312e7bdd, e2dc7765", "build": _b_shell,
                  "say": lambda v: f"Run the shell command `{str(v.get('script') or '')[:120]}`"},
    "menu": {"ids": [A + "choosefrommenu"], "does": "let the user pick from a menu; each choice has its own steps",
             "values": {"prompt": "the question", "options": "[{title, steps:[...]}]"}, "out": "Menu Result",
             "source": "Apple Gallery DocumentReview; iCloud 523fa506", "build": _b_menu,
             "say": lambda v: f"Ask you to choose: {', '.join(str(o.get('title')) for o in v.get('options') or [] if isinstance(o, dict))}"},
    "repeat": {"ids": [A + "repeat.count"], "does": "do some steps several times",
               "values": {"times": "how many", "steps": "[...]"}, "out": "Repeat Results",
               "source": "Apple Gallery DocumentReview; iCloud 3a98d96d", "build": _b_repeat,
               "say": lambda v: f"Repeat {int(_number(v.get('times') or 1, 1, 100))} times"},
}

OUTPUT_ID = A + "output"           # Stop and Output (Apple Gallery-style; iCloud e066c42a, d1e0c5d6)


def _step_parts(step) -> tuple[str, dict]:
    if not isinstance(step, dict):
        raise PlanError(f"a step is not understood: {str(step)[:60]}")
    key = str(step.get("do") or step.get("action") or "").strip().lower()
    values = step.get("with") if isinstance(step.get("with"), dict) else {
        k: v for k, v in step.items() if k not in ("do", "action")}
    return key, values


def _build_steps(steps: list, b: _Builder, top: bool = True) -> None:
    for number, step in enumerate(steps, 1):
        key, values = _step_parts(step)
        spec = CATALOG.get(key)
        if spec is None:
            raise PlanError(f"'{key}' is not an action in Mint's catalog")
        before = b.last
        spec["build"](values, b)
        if top and b.last is not None and b.last != before and spec.get("out"):
            b.steps[number] = b.last


def workflow(steps: list) -> dict:
    """The shortcut plist for catalog steps; raises PlanError."""
    b = _Builder()
    _build_steps(steps, b)
    if not b.actions:
        raise PlanError("there are no steps")
    final = b.said if b.said is not None else (b.text("{prev}") if b.last else None)
    if final is not None:
        b.add(OUTPUT_ID, {"WFOutput": final})     # so `shortcuts run` (and Mint's Run) get the result
    inputs = ["WFAppContentItem", "WFArticleContentItem", "WFContactContentItem", "WFDateContentItem",
              "WFEmailAddressContentItem", "WFGenericFileContentItem", "WFImageContentItem", "WFiTunesProductContentItem",
              "WFLocationContentItem", "WFDCMapsLinkContentItem", "WFAVAssetContentItem", "WFPDFContentItem",
              "WFPhoneNumberContentItem", "WFRichTextContentItem", "WFSafariWebPageContentItem", "WFStringContentItem",
              "WFURLContentItem"]
    return {"WFWorkflowActions": b.actions, "WFWorkflowClientVersion": "4042.0.2.2",
            "WFWorkflowMinimumClientVersion": 900, "WFWorkflowMinimumClientVersionString": "900",
            "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
            "WFWorkflowImportQuestions": [], "WFWorkflowInputContentItemClasses": inputs, "WFWorkflowTypes": [],
            "WFWorkflowOutputContentItemClasses": [], "WFWorkflowHasOutputFallback": False,
            "WFWorkflowHasShortcutInputVariables": b.uses_input, "WFQuickActionSurfaces": []}


def describe(steps: list, depth: int = 0) -> list[str]:
    """The steps in plain words, numbered, nested steps indented."""
    lines = []
    for number, step in enumerate(steps, 1):
        key, values = _step_parts(step)
        spec = CATALOG.get(key)
        words = spec["say"](values) if spec else f"({key}?)"
        lines.append(("   " * depth) + f"{number}. {words}")
        if key == "menu":
            for option in values.get("options") or []:
                if isinstance(option, dict):
                    lines.append(("   " * (depth + 1)) + f"If you choose “{option.get('title')}”:")
                    lines += describe(option.get("steps") or [], depth + 2)
        elif key == "repeat":
            lines += describe(values.get("steps") or [], depth + 1)
    return lines


def _flags(steps: list) -> list[str]:
    said = []
    for step in steps:
        key, values = _step_parts(step)
        flag = (CATALOG.get(key) or {}).get("flag")
        if flag == "shell":
            said.append(f"It runs a shell command on this Mac: `{str(values.get('script') or '')[:160]}`. "
                        "(Shortcuts needs 'Allow Running Scripts' on in its Settings > Advanced.)")
        elif flag == "email":
            said.append(f"It opens an email draft to {values.get('to') or 'no one yet'} - nothing is sent until you "
                        "press Send yourself.")
        for option in values.get("options") or []:
            if isinstance(option, dict):
                said += _flags(option.get("steps") or [])
        if key == "repeat":
            said += _flags(values.get("steps") or [])
    return said


# --- Planning ----------------------------------------------------------------------------------

def _catalog_text() -> str:
    lines = []
    for key, spec in CATALOG.items():
        values = "; ".join(f"{name}: {about}" if about else name for name, about in spec["values"].items())
        result = f" -> result '{spec['out']}'" if spec.get("out") else ""
        lines.append(f"- {key}: {spec['does']}{result}" + (f" [{values}]" if values else ""))
    return "\n".join(lines)


_PLAN = """You turn a request for an Apple Shortcut (macOS Shortcuts app) into steps. Use ONLY these actions \
(nothing else exists for you):
{catalog}

Values can use earlier results: {{prev}} = the result of the step just before, {{step N}} = the result of top-level \
step N, {{date}} = the current date, {{input}} = what the shortcut is given, {{repeat index}} = inside a repeat. \
For "today's date" use date_text (show: date) then {{prev}}. Percentages are 0-100. A menu's steps and a repeat's \
steps go inside it.

Rules:
- If part of the request needs an action that is NOT in the list (sending a message or email, deleting files, \
controlling other devices, Home accessories, reading files, a specific song/album, ...), leave that part out and \
say it in "missing" as: Shortcuts has no action for <that> in Mint's catalog. If what is left would be \
pointless without that part, give no steps at all.
- email_draft only opens a draft; never claim it sends. Use run_shell only when the user clearly asked for a \
command or script, never to delete, kill, change system settings or run as administrator.
- If the whole request is harmful (malware, spying, harassment, destroying data, getting around security), set \
"refuse" to a short reason and give no steps.
- name: a short Title Case name for the shortcut (2-4 words), not starting with "Mint".

Answer JSON only: {{"name": "...", "steps": [{{"do": "<action>", "with": {{...}}}}], "missing": ["..."], \
"refuse": ""}}

The request: {request}"""


def _unique_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "", " ".join(str(name or "").split()))[:40].strip() or "My Shortcut"
    if name.lower().startswith("mint "):
        name = name[5:].strip() or "My Shortcut"
    try:
        from mint.tools import shortcut_library
        taken = {n.lower() for n in shortcut_library.installed(fresh=True)}
    except Exception:
        taken = set()
    taken |= {p["name"].lower() for p in _state["plans"].values() if p.get("created")}
    final, n = name, 2
    while final.lower() in taken:
        final, n = f"{name} {n}", n + 1
    return final


def plan(description: str) -> dict:
    """-> {id, name, steps, lines, warnings, missing, refuse, error}. Checks the steps by building them."""
    from mint.core import llm
    description = " ".join(str(description or "").split())
    if not description:
        return {"error": "Say what the shortcut should do."}
    try:
        text, _model = llm.generate(_PLAN.format(catalog=_catalog_text(), request=description[:1500]), MODELS,
                                    json_mode=True)
        answer = llm.parse_json(text)
    except Exception as error:
        log.info("plan: %s", error)
        return {"error": f"Could not plan it right now ({str(error)[:120]})."}
    if not isinstance(answer, dict):
        return {"error": "The planner gave no plan; try describing it differently."}
    refuse = str(answer.get("refuse") or "").strip()
    missing = [str(m).strip() for m in (answer.get("missing") or []) if str(m).strip()]
    steps = answer.get("steps") if isinstance(answer.get("steps"), list) else []
    result = {"description": description, "refuse": refuse, "missing": missing, "steps": [], "lines": [],
              "warnings": [], "error": ""}
    if refuse:
        return result
    kept = []
    for step in steps:
        try:
            key, _values = _step_parts(step)
        except PlanError:
            continue
        if key in CATALOG:
            kept.append(step)
        else:
            missing.append(f"Shortcuts has no action for '{key}' in Mint's catalog")
    if not kept:
        result["error"] = "None of it can be done with the actions Mint knows." if missing else \
            "The planner gave no steps; try describing it differently."
        return result
    try:
        workflow(kept)                                    # every value checked now, not at create time
    except PlanError as error:
        result["error"] = f"That plan doesn't work: {error}."
        return result
    except Exception as error:
        log.exception("plan build")
        result["error"] = f"That plan doesn't work: {error}."
        return result
    result.update(id=uuid.uuid4().hex[:8], name=_unique_name(answer.get("name") or description[:30]), steps=kept,
                  lines=describe(kept), warnings=_flags(kept), made=time.time())
    with _lock:
        try:
            from mint.app import live
            result["asked"] = " ".join((live.request() or "").lower().split())   # the request it was planned for
        except Exception:
            result["asked"] = ""
        _state["plans"][result["id"]] = result
        _state["last"] = result["id"]
    return result


def plan_text(result: dict) -> str:
    """The plan in words (for the model to read back, and for Settings)."""
    if result.get("refuse"):
        return f"REFUSED: Mint won't make that shortcut ({result['refuse']})."
    if result.get("error"):
        extra = (" " + " ".join(result.get("missing") or [])) if result.get("missing") else ""
        return f"FAILED: {result['error']}{extra}"
    words = f"Plan '{result['id']}' for a shortcut named '{result['name']}':\n" + "\n".join(result["lines"])
    for warning in result["warnings"]:
        words += f"\nNote: {warning}"
    for gap in result["missing"]:
        words += f"\nCan't do: {gap}"
    return words


# --- Creating ------------------------------------------------------------------------------------

def on_status(listener) -> None:
    """listener(name, status) - 'waiting', 'added' or 'not added' - on a background thread."""
    _state["listeners"].append(listener)


def _set_status(name: str, status: str) -> None:
    _state["status"][name] = status
    for listener in list(_state["listeners"]):
        try:
            listener(name, status)
        except Exception:
            log.exception("shortcut status listener")


def status(name: str) -> str:
    return _state["status"].get(name, "")


def _watch(name: str, seconds: float = POLL_SECONDS) -> None:
    """Wait (in the background) for the user's "Add Shortcut" click, then say so."""
    from mint.tools import shortcuts as apple_shortcuts
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(4)
        try:
            if name in apple_shortcuts.names(fresh=True):
                _set_status(name, "added")
                try:
                    from mint.tools import cards
                    cards.show(f"Added '{name}'", subtitle=f"say “run {name}”", icon="square.stack.3d.up.fill",
                               tint="green", seconds=15)
                except Exception as error:
                    log.info("card: %s", error)
                return
        except Exception as error:
            log.info("watch %s: %s", name, error)
    _set_status(name, "not added")


def build(result: dict, folder=None):
    """Sign the plan's shortcut. -> the signed file path; raises RuntimeError / PlanError."""
    from mint.tools import shortcut_library
    return shortcut_library.sign(workflow(result["steps"]), result["name"], folder)


def create(plan_id: str = "", name: str = "", watch: bool = True) -> str:
    with _lock:
        result = _state["plans"].get(plan_id or _state.get("last") or "")
    if result is None:
        return "FAILED: there is no plan to make yet - plan it first (action=plan), read it back, then create."
    if name:
        result["name"] = _unique_name(name)
    try:
        signed = build(result)
    except Exception as error:
        return f"FAILED: could not build the shortcut ({error})."
    from mint.tools import shortcut_library
    shortcut_library.open_for_import(signed)
    result["created"] = True
    _set_status(result["name"], "waiting")
    if watch:
        threading.Thread(target=_watch, args=(result["name"],), daemon=True, name="shortcut-watch").start()
    return (f"NOT DONE YET: opened '{result['name']}' in Shortcuts. The user clicks 'Add Shortcut' there (once - Mint "
            f"can't); Mint shows a card when it's added, then 'run {result['name']}' works. Tell them in one "
            "sentence; don't click it for them.")


# --- The tool -------------------------------------------------------------------------------------

def _not_agreed_yet(plan_id: str) -> str:
    """By voice, a shortcut is made only after the user answers the plan: the model asked "Should I go ahead?" and
    then made it in the same breath, with the request saying "don't create it yet" (30 Sep). The Settings Create
    button is the user's own click and calls create() directly."""
    with _lock:
        result = _state["plans"].get(plan_id or _state.get("last") or "")
    if result is None:
        return ""
    from mint.app import live
    now = " ".join((live.request() or "").lower().split())
    if result.get("asked") and now == result["asked"]:
        return ("NOT YET: the user hasn't answered the plan. Read the steps back, ask whether to make it, and wait "
                "for their yes (a new message) before action=create.")
    return ""


def tool(args: dict) -> str:
    action = str(args.get("action") or "plan").lower()
    try:
        if action == "plan":
            result = plan(str(args.get("description") or args.get("prompt") or ""))
            words = plan_text(result)
            if result.get("steps"):
                words += ("\nRead the steps back briefly (and any note) and ask whether to make it. Only if they say "
                          f"yes: make_shortcut action=create plan_id={result['id']}.")
            return words
        if action == "create":
            waiting = _not_agreed_yet(str(args.get("plan_id") or ""))
            if waiting:
                return waiting
            return create(str(args.get("plan_id") or ""), str(args.get("name") or ""))
        from mint.tools import shortcut_library
        if action == "library":
            return shortcut_library.library_text()
        if action == "add":
            name = str(args.get("name") or "").strip()
            return shortcut_library.offer_missing() if name.lower() in ("", "all") else shortcut_library.offer(name)
        return f"FAILED: make_shortcut has no action '{action}' (plan, create, library, add)."
    except Exception as error:
        log.exception("make_shortcut")
        return f"FAILED: {error}"


PROMPT = """Making Apple Shortcuts: "make me a shortcut that ...", "create a shortcut to ..." -> make_shortcut \
action=plan description=<their words>. Read the numbered steps back in a sentence or two (plus any Note or Can't \
do) and ask if you should make it; ONLY after a yes -> action=create plan_id=<id>. Mint opens it in the Shortcuts \
app and the user clicks 'Add Shortcut' once (you can't); a card says when it's added, then shortcut action=run \
runs it. If they change the idea, plan again. action=library lists Mint's own shortcuts and the user's; \
action=add name=<one of Mint's, or all> offers a missing one of Mint's. These are Shortcuts-app shortcuts, not \
keyboard shortcuts."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="make_shortcut",
        description=("Make a new Apple Shortcut (Shortcuts app) from a plain description: plan the steps first "
                     "and read them back, create only after the user agrees (they click 'Add Shortcut' once). Also "
                     "list Mint's shortcuts and the user's, or offer one of Mint's own that is missing."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["plan", "create", "library", "add"]),
            "description": types.Schema(type=S, description="plan: what the shortcut should do, in the user's words"),
            "plan_id": types.Schema(type=S, description="create: the id from plan (empty = the last plan)"),
            "name": types.Schema(type=S, description="create: a different name; add: which of Mint's shortcuts, "
                                                     "or all")},
            required=["action"]))]


HANDLERS = {"make_shortcut": tool}


if __name__ == "__main__":        # a quick look at the catalog
    print(json.dumps({k: v["ids"] for k, v in CATALOG.items()}, indent=1))
