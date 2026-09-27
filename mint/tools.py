"""Tool declarations and dispatch.

Tier 0 tools run as direct system calls and return in milliseconds.
Tier 1 is `desktop`, which hands a goal to Desktop Voice / Jev.
Tier 2 is `look`, which captures the screen for the model to inspect.
"""

from __future__ import annotations

import asyncio
import logging
import time

from google.genai import types

from . import apps, config, custom, documents, fastinput, jev, macos, ocr, skills, vision, workspace
from .desktop import bridge

log = logging.getLogger("mint.tools")


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None):
    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                key: types.Schema(**value) for key, value in properties.items()
            },
            required=required or [],
        ),
    )


STRING = {"type": types.Type.STRING}
INTEGER = {"type": types.Type.INTEGER}


def declarations() -> list[types.FunctionDeclaration]:
    functions = [
        # --- Tier 0 ----------------------------------------------------------
        _fn("open_app", "Instantly launch or switch to a Mac application by name.",
            {"name": {**STRING, "description": "Application name, e.g. 'Finder', 'Safari', 'Notes'."}},
            ["name"]),
        _fn("open_url", "Instantly open a web address in a new browser tab (reuses a tab already showing it). "
            "The browser is picked for you: one the user names, else the user's browser rules (e.g. "
            "entertainment in Brave), else the browser in front. Don't open a browser with open_app first.",
            {"url": {**STRING, "description": "Address such as 'youtube.com' or 'https://example.com'."},
             "browser": {**STRING, "description": "Only when the user named a browser: Chrome, Brave, Safari..."}},
            ["url"]),
        _fn("open_folder", "Instantly open a folder in Finder.",
            {"name": {**STRING, "description": "Folder name such as 'Downloads', or a full path."}},
            ["name"]),
        _fn("scroll", "Instantly scroll the window under the pointer.",
            {"direction": {**STRING, "description": "up, down, left or right."},
             "amount": {**INTEGER, "description": "Number of scroll steps; about 5 is one screen. Default 3."}},
            ["direction"]),
        _fn("press_key", "Instantly press a key, optionally with modifiers.",
            {"key": {**STRING, "description": "return, escape, tab, space, up, down, left, right, a letter or a digit."},
             "modifiers": {"type": types.Type.ARRAY, "items": {"type": types.Type.STRING},
                           "description": "Any of command, control, option, shift."},
             "times": {**INTEGER, "description": "How many presses. Default 1."}},
            ["key"]),
        _fn("set_volume", "Instantly set the Mac's output volume.",
            {"level": {**INTEGER, "description": "0 to 100."}}, ["level"]),
        _fn("media_key", "Instantly send a media key: play/pause, next or previous track.",
            {"action": {**STRING, "description": "playpause, next or previous."}}, ["action"]),
        _fn("frontmost_app", "Which application is in front right now.", {}),
        _fn("list_windows", "List the visible windows as app name and title.", {}),
        _fn("read_clipboard", "Read the text currently on the clipboard.", {}),
        _fn("write_clipboard", "Put text on the clipboard.",
            {"text": {**STRING, "description": "Text to copy."}}, ["text"]),
        _fn("quit_app", "Ask an application to quit. Confirm with the user first if work may be unsaved.",
            {"name": {**STRING, "description": "Application name."}}, ["name"]),

        # --- Skills (also instant) ----------------------------------------------
        _fn("type_text",
            "Instantly insert text at the cursor in whatever field is focused, in any app. "
            "Use for dictation and for writing text you composed. Much faster than the desktop tool "
            "when the right field is already focused.",
            {"text": {**STRING, "description": "Exactly the text to insert."},
             "press_return": {"type": types.Type.BOOLEAN,
                              "description": "Press Return afterwards, e.g. to submit a search. Never to send a message unless asked."}},
            ["text"]),
        _fn("get_selected_text",
            "Read the text the user has selected in the front app. Use when they say 'this', "
            "'the selected text', 'summarise this', 'reply to this', 'translate this'.", {}),
        _fn("get_status", "Current local time and date, battery level, and volume.", {}),
        _fn("set_timer",
            "Start a timer. When it ends, the user gets a notification and you announce it.",
            {"minutes": {"type": types.Type.NUMBER, "description": "Length in minutes; 0.5 is thirty seconds."},
             "label": {**STRING, "description": "What the timer is for, e.g. 'tea'."}},
            ["minutes"]),
        _fn("notify", "Show a macOS notification.",
            {"title": {**STRING}, "message": {**STRING}}, ["title"]),
        _fn("system_action", "Lock the screen, sleep the display, switch dark mode, or mute.",
            {"action": {**STRING, "description":
                        "lock, sleep_display, dark_mode_on, dark_mode_off, toggle_dark_mode, mute, unmute."}},
            ["action"]),
        _fn("calendar_events", "The user's calendar events from today onwards.",
            {"days": {**INTEGER, "description": "How many days to cover, starting today. Default 1."}}),
        _fn("create_reminder",
            "Create a reminder in the Reminders app, optionally due at a time. Call get_status first "
            "if you need the current time to work out the due time.",
            {"title": {**STRING},
             "in_minutes": {**INTEGER, "description": "Due this many minutes from now."},
             "when": {**STRING, "description": "Due at a local date-time, ISO format, e.g. 2026-09-24T09:00."}},
            ["title"]),
        _fn("create_note", "Create a note in the Notes app.",
            {"title": {**STRING}, "body": {**STRING}}, ["title"]),
        _fn("compose_email",
            "Open a pre-filled email draft in the user's mail app for them to review and send. "
            "It never sends. Write the body yourself from what the user asked.",
            {"to": {**STRING, "description": "Recipient address, if known."},
             "subject": {**STRING}, "body": {**STRING}}),
        _fn("list_emails",
            "List the newest messages in the macOS MAIL APP's inbox. Only when the user asks about "
            "the Mail app or does not care which account. For Gmail or a named account (e.g. "
            "'my acme Gmail'), use open_chrome with that account and read_window instead: "
            "the Mail app may hold other accounts' mail.",
            {"count": {**INTEGER, "description": "How many, default 5."}}),
        _fn("read_email", "Read one message from the macOS Mail app's inbox in full (see list_emails).",
            {"number": {**INTEGER, "description": "Its position from list_emails, 1 = newest."}},
            ["number"]),

        # --- Accounts, Slack, routines --------------------------------------------
        _fn("open_chrome",
            "Open Google Chrome as one particular account (Chrome profile), optionally at a URL. "
            "Use whenever the user names an account, email or profile: 'open my acme Chrome', "
            "'open Gmail in alex@example.com'. Pass their words for the account; it is matched "
            "to a real profile, and if that is ambiguous the result lists the choices to ask about.",
            {"account": {**STRING, "description": "The account as the user said it: an email, name or nickname."},
             "url": {**STRING, "description":
                     "Optional plain address, e.g. mail.google.com, docs.new, calendar.google.com, "
                     "notion.so. The profile already selects the account: never put an email or "
                     "account in the URL (mail.google.com/mail/u/<email> shows Gmail's error page)."}},
            ["account"]),
        _fn("open_slack",
            "Open Slack, optionally switch to a workspace, and optionally jump to a channel, DM or person "
            "by name. Use this for anything in Slack rather than the desktop tool.",
            {"workspace": {**STRING, "description": "Workspace as the user said it. Omit to stay in the current one."},
             "channel": {**STRING, "description": "Channel or person to open, e.g. 'on-call' or 'Priya'."}}),
        _fn("list_accounts", "List the Chrome profiles and Slack workspaces available.", {}),
        _fn("read_window",
            "Read ALL the text in the front window - a web page, a Gmail inbox or open email, a "
            "document, an app - exactly, including parts scrolled out of view. Use it to read "
            "anything before summarising, extracting or copying it somewhere else. Prefer it over "
            "look whenever the goal is the words rather than the picture.",
            {"max_chars": {**INTEGER, "description": "Cap on text returned. Default 12000."}}),
        _fn("list_open",
            "What is open right now: the app in front, and every Chrome window with its profile "
            "and tabs (* = the selected tab). Check this BEFORE opening something - it may already "
            "be open, and reusing it is what a person would do.", {}),
        _fn("switch_to",
            "Bring an already-open Chrome tab or window to the front, by words from its title, "
            "e.g. 'Inbox acme' or 'Untitled document'. Works across Spaces.",
            {"what": {**STRING}}, ["what"]),
        _fn("export_doc_pdf",
            "Export the open Google Doc itself as a PDF, exactly as Docs renders it, and open it. "
            "Use this - not create_pdf - when the user asks to export or download a Google Doc as PDF.",
            {"open_after": {"type": types.Type.BOOLEAN}}),
        _fn("plan_task",
            "Start a multi-step task: state the goal and the ordered steps BEFORE doing any of "
            "them. Use for anything with three or more steps. The steps are tracked and shown to "
            "the user, and every tool result will remind you what comes next.",
            {"goal": {**STRING},
             "steps": {"type": types.Type.ARRAY, "items": {"type": types.Type.STRING},
                       "description": "Short imperative steps in order, e.g. 'Read today's calendar'."}},
            ["goal", "steps"]),
        _fn("step_done",
            "Mark a step of the current task finished (or failed), with a one-line result.",
            {"step": {**INTEGER, "description": "Step number, 1-based."},
             "result": {**STRING},
             "failed": {"type": types.Type.BOOLEAN}},
            ["step", "result"]),
        _fn("create_pdf",
            "Write a nicely formatted PDF and open it. Compose the full content yourself. Content "
            "uses light Markdown: '# Heading', '- bullet', '**bold**', blank lines between "
            "paragraphs. Saved to ~/Documents/Mint.",
            {"title": {**STRING}, "content": {**STRING},
             "open_after": {"type": types.Type.BOOLEAN, "description": "Open it when done. Default true."}},
            ["title", "content"]),
        _fn("run_routine",
            "Run one of the user's saved routines by name. The available routines are listed in "
            "your instructions.",
            {"name": {**STRING, "description": "The routine, as the user said it."}},
            ["name"]),

        # --- Tier 1 ----------------------------------------------------------
        _fn("desktop",
            "Drive the screen to do something that the instant tools cannot: click a specific "
            "button or link, type into a particular field, choose a menu item, pick a search "
            "result. Reads the screen through the accessibility tree and reports what actually "
            "happened. Takes about a second per step, so prefer the instant tools when they fit. "
            "Give one clear goal in plain words, and name targets unambiguously.",
            {"goal": {**STRING, "description":
                      "One goal, e.g. 'click the Sign in button' or "
                      "'type lo-fi into the search box and press return'."}},
            ["goal"]),

        # --- Tier 2 ----------------------------------------------------------
        _fn("set_preference",
            "Change how you (Mint) behave or look, when the user asks: 'don't speak, just "
            "chat' -> spoken_replies off; 'talk to me again' / 'speak' -> spoken_replies on; "
            "'turn off the mic' -> microphone off (then they can only type); 'make it purple' "
            "-> theme; 'move to the bottom left' -> position; also the orb's face, the on-screen "
            "effects, word-by-word captions, and whether you listen while you work. Confirm "
            "briefly what you changed.",
            {"setting": {**STRING, "enum": ["spoken_replies", "microphone", "theme", "position",
                                            "face", "effects", "word_animation", "listen_while_working"]},
             "value": {**STRING, "description":
                       "on/off for switches; theme: mint (green), blue, aurora (teal/violet), sunset "
                       "(orange), rose (pink), mono (white); position: top-right, "
                       "top-left, top-center, bottom-right, bottom-left."}},
            ["setting", "value"]),

        _fn("chat_action",
            "Manage the chat window's conversation when the user asks: 'clear the chat' -> clear "
            "(window only; memory is kept); 'summarise / compact our conversation' -> summarize "
            "(a Flash model summarises it, a summary card appears, and you are restarted with just "
            "that summary as context - like compacting); 'start a new session / start "
            "fresh / new conversation' -> new_session (saves this conversation to memory, then "
            "restarts you with a clean context).",
            {"action": {**STRING, "enum": ["clear", "summarize", "new_session"]}}, ["action"]),
        _fn("show_chat",
            "Open or close the chat window (conversation history, typing box, settings). Use "
            "when the user says 'show me the chat', 'open the chat', 'hide the chat'.",
            {"open": {"type": types.Type.BOOLEAN}}, ["open"]),

        _fn("stop_listening",
            "Go back to sleep and wait for the wake word. Call this when the user says "
            "they are done - 'that's all', 'go to sleep', 'stop listening', 'thanks, bye'. "
            "Say a short goodbye first.", {}),

        _fn("look",
            "Capture the screen and look at it. Use when you need to SEE something: read a chart "
            "or image, judge a layout, work out why an action did not behave as expected, or find "
            "a control the desktop tool could not. Do not call it routinely before acting.",
            {"display": {**INTEGER, "description":
                         "Which display: 0 for everything, 1 for the main one. Default 0."}}),
        _fn("ui_act",
            "THE way to click, type into or pick anything in an app's window. It reads every "
            "control on screen (buttons, fields, links, menu items - with their real roles, so a "
            "button is never confused with a heading of the same words), picks the one you "
            "describe (an open dialog always wins), moves the pointer there and clicks like a "
            "person, then checks what changed. For typing it clicks the field, pastes the text "
            "and checks the field holds it. One control per call; call again for the next step.",
            {"action": {**STRING, "enum": ["click", "type", "double_click", "right_click", "dismiss"],
                        "description": "dismiss closes the open dialog, menu or popup (Escape, its close "
                                       "button, or a click beside it); target can be empty."},
             "target": {**STRING, "description":
                        "The control in plain words, as specific as you can: 'Create project button "
                        "in the dialog', 'Project name field', 'New project in the menu', 'the chat "
                        "message box', 'Choose project'."},
             "text": {**STRING, "description": "For type: exactly what to type."},
             "press_return": {"type": types.Type.BOOLEAN, "description": "For type: press Return after."},
             "app": {**STRING, "description": "Optional: the app it is in, if not the one in front. "
                                            "'Mint' for your own chat window and its buttons."}},
            ["action", "target"]),
        _fn("ui_elements",
            "List the controls in the front window right now (numbered, with role, label and "
            "where they are; says if a dialog is open). Use it when you are unsure what is on "
            "screen or what to call a control - it is exact, unlike a screenshot.", {}),
        _fn("click_text",
            "Click a piece of text you can name on screen - a sidebar item, tab, button or link "
            "label - in apps the desktop tool cannot see into (Slack, some Electron apps). The "
            "screen's text is read on this Mac and the label is matched exactly or by a classifier, "
            "so it can only click text that is really there. It reports whether the screen changed.",
            {"text": {**STRING, "description": "The label as it appears, or as the user described it, e.g. 'Huddles'."},
             "double": {"type": types.Type.BOOLEAN, "description": "Double-click."}},
            ["text"]),
        _fn("click_at",
            "Click a point you can see in the latest look screenshot. Coordinates are 0-1000 "
            "across the screenshot (x from left, y from top). This is the FALLBACK for controls "
            "the desktop tool cannot find, because the app hides them from Accessibility (Slack, "
            "canvases, games, some Electron apps). Prefer the desktop tool whenever it works.",
            {"x": {"type": types.Type.NUMBER, "description": "0-1000 from the left edge."},
             "y": {"type": types.Type.NUMBER, "description": "0-1000 from the top edge."},
             "button": {**STRING, "description": "left (default) or right."},
             "double": {"type": types.Type.BOOLEAN, "description": "Double-click."}},
            ["x", "y"]),
    ]

    if config.ALLOW_SHELL:
        functions.append(
            _fn("run_shell",
                "Run a shell command on the Mac and return its output. Use for file and system "
                "work that has no better tool. Never run destructive commands without asking.",
                {"command": {**STRING, "description": "The command to run."}}, ["command"])
        )
    return functions


def tools() -> list[types.Tool]:
    result = [types.Tool(function_declarations=declarations())]
    if config.ALLOW_SEARCH:
        result.insert(0, types.Tool(google_search=types.GoogleSearch()))
    return result


# --- Dispatch ----------------------------------------------------------------

_SYNC = {
    "open_app": lambda a: macos.open_app(a["name"]),
    "open_url": lambda a: macos.open_url(a["url"], a.get("browser", "")),
    "open_folder": lambda a: macos.open_folder(a["name"]),
    "scroll": lambda a: fastinput.scroll(a.get("direction", "down"), a.get("amount", 3)),
    "press_key": lambda a: fastinput.press_key(a["key"], a.get("modifiers"), a.get("times", 1)),
    "set_volume": lambda a: fastinput.set_volume(a["level"]),
    "media_key": lambda a: fastinput.media_key(a["action"]),
    "frontmost_app": lambda a: macos.frontmost_app(),
    "list_windows": lambda a: macos.list_windows(),
    "read_clipboard": lambda a: macos.read_clipboard(),
    "write_clipboard": lambda a: macos.write_clipboard(a["text"]),
    "quit_app": lambda a: macos.quit_app(a["name"]),
    "run_shell": lambda a: macos.run_shell(a["command"]),
    "type_text": lambda a: skills.type_text(a["text"], bool(a.get("press_return", False))),
    "get_selected_text": lambda a: skills.get_selected_text(),
    "get_status": lambda a: skills.get_status(),
    "notify": lambda a: skills.notify(a["title"], a.get("message", "")),
    "system_action": lambda a: skills.system_action(a["action"]),
    "calendar_events": lambda a: skills.calendar_events(a.get("days", 1)),
    "create_reminder": lambda a: skills.create_reminder(a["title"], a.get("in_minutes"), a.get("when")),
    "create_note": lambda a: skills.create_note(a["title"], a.get("body", "")),
    "compose_email": lambda a: skills.compose_email(a.get("to", ""), a.get("subject", ""), a.get("body", "")),
    "list_emails": lambda a: skills.list_emails(a.get("count", 5)),
    "read_email": lambda a: skills.read_email(a["number"]),
    "list_accounts": lambda a: apps.list_accounts(),
    "click_text": lambda a: ocr.click_text(a["text"], bool(a.get("double", False))),
    "ui_act": lambda a: _ui_act(a),
    "ui_elements": lambda a: __import__("mint.ground", fromlist=["ground"]).summary(),
    # Not offered to the model: the grounding benchmark, run through the app for its permissions.
    "ground_bench": lambda a: __import__("mint.groundbench", fromlist=["run"]).run(a),
    "read_window": lambda a: documents.read_window(int(a.get("max_chars", 12000))),
    "list_open": lambda a: workspace.describe(),
    "switch_to": lambda a: workspace.switch_to(a["what"]),
    "export_doc_pdf": lambda a: documents.export_doc_pdf(a.get("open_after", True) is not False),
    "create_pdf": lambda a: documents.create_pdf(a["title"], a["content"], a.get("open_after", True) is not False),
    "click_at": lambda a: vision.click_at(
        float(a["x"]), float(a["y"]), a.get("button", "left"), bool(a.get("double", False))),
}


def _fallback_in(loop: asyncio.AbstractEventLoop):
    """A Desktop Voice runner callable from a worker thread: the bridge lives on `loop`."""
    def run(goal: str) -> str:
        return asyncio.run_coroutine_threadsafe(bridge.run_goal(goal), loop).result(timeout=120)
    return run


async def _open_slack(args: dict) -> str:
    fallback = _fallback_in(asyncio.get_running_loop())
    return await asyncio.to_thread(
        apps.open_slack, args.get("workspace", ""), args.get("channel", ""), fallback)


async def _open_chrome(args: dict) -> str:
    fallback = _fallback_in(asyncio.get_running_loop())
    return await asyncio.to_thread(
        apps.open_chrome, args.get("account", ""), args.get("url", ""), fallback)


async def _run_routine(name: str) -> str:
    found = custom.routines()
    if not found:
        return "There are no routines. They are defined in custom.json under 'routines'."
    options = {n: f"routine '{n}'" + (f": {s.get('description')}" if s.get("description") else "")
               for n, s in found.items()}
    chosen, why = jev.resolve(name, options, what="routine")
    if chosen is None:
        return why
    results = []
    for number, step in enumerate(found[chosen].get("steps", []), 1):
        step = dict(step)
        tool = step.pop("tool", "")
        result, _ = await dispatch(tool, step)
        results.append(f"{number}. {tool}: {result}")
    return f"Ran '{chosen}':\n" + "\n".join(results)


def _ui_act(args: dict) -> str:
    from . import ground
    if args.get("app"):
        import AppKit
        from . import axkit
        wanted = str(args["app"]).lower()
        app = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                    if (a.localizedName() or "").lower() == wanted), None)
        from . import prefs
        if wanted in ("mint", "jarvis", prefs.name().lower()):
            return ground.own_act(str(args.get("action", "click")), str(args.get("target", "")),
                                  str(args.get("text", "")))
        if app is None:
            return f"FAILED: {args['app']} is not running; open it first."
        if not ground.bring_forward(app):
            return f"FAILED: could not bring {args['app']} to the front, so nothing was done."
    if ground.OWN_CHAT_OPEN and not args.get("app") and str(args.get("action", "click")) == "click":
        # Mint's chat is open: "the Sleep button" means Mint's own, if it has one.
        target = set(ground._words(str(args.get("target", ""))))
        if any(set(ground._words(c["label"])) & target - {"button", "the", "in", "chat"} for c in ground.own_controls()):
            result = ground.own_act("click", str(args.get("target", "")))
            if not result.startswith("FAILED"):
                return result
    return ground.act(str(args.get("action", "click")), str(args.get("target", "")),
                      str(args.get("text", "")), bool(args.get("press_return", False)))


_KEY_WORDS = {
    "return": "Return", "enter": "Return", "escape": "Escape", "esc": "Escape",
    "space": "Space", "tab": "Tab", "up": "up arrow", "down": "down arrow",
    "left": "left arrow", "right": "right arrow",
}


def _fallback_goal(name: str, args: dict) -> str | None:
    """The same action phrased for the desktop tool, or None if it cannot express it.

    Without Accessibility, synthesised keys and scrolls are silently dropped.
    Desktop Voice holds that permission itself, so it can do the same thing a
    little slower. Relying on the model to make this switch failed in testing:
    it told the user to grant permission instead.
    """
    if name == "scroll":
        times = max(1, round(int(args.get("amount", 3)) / 4))
        return f"scroll {args.get('direction', 'down')} {times} times"
    if name == "press_key" and not args.get("modifiers"):
        key = _KEY_WORDS.get(str(args.get("key", "")).lower())
        return f"press {key}" if key else None
    if name == "media_key" and str(args.get("action", "")).lower() == "playpause":
        return "press Space to play or pause"
    if name == "type_text":
        goal = f"type {args.get('text', '')} into the focused text field"
        return goal + (" and press Return" if args.get("press_return") else "")
    return None


# Opening an app or a site returns as soon as it is requested, not when the
# window is ready. The model often issues "open" and the next action in the same
# breath; in testing a scroll landed 66ms after a URL opened and hit nothing.
_OPENERS = {"open_app", "open_url", "open_folder", "open_chrome", "open_slack"}
_SCREEN_ACTIONS = {"scroll", "press_key", "type_text", "get_selected_text", "desktop", "look",
                   "media_key", "click_at", "click_text", "read_window", "export_doc_pdf",
                   "ui_act", "ui_elements"}
_SETTLE = 2.0
_last_open = 0.0


async def dispatch(name: str, args: dict) -> tuple[str, dict | None]:
    """Run one tool call.

    Returns the text result and, for `look`, an image part to send alongside it.
    """
    global _last_open
    log.info("tool %s %s", name, args)

    if name in _SCREEN_ACTIONS:
        wait = _SETTLE - (time.monotonic() - _last_open)
        if wait > 0:
            await asyncio.sleep(wait)
    if name in _OPENERS:
        _last_open = time.monotonic()

    if name in {"scroll", "press_key", "media_key", "type_text", "get_selected_text"} \
            and not fastinput.has_accessibility():
        goal = _fallback_goal(name, args or {})
        if goal is None:
            return (f"Cannot {name.replace('_', ' ')} without Accessibility permission. "
                    "Tell the user to run: ./run.sh --grant"), None
        return await bridge.run_goal(goal), None

    if name == "open_slack":
        return await _open_slack(args or {}), None
    if name == "open_chrome":
        return await _open_chrome(args or {}), None
    if name == "run_routine":
        return await _run_routine((args or {}).get("name", "")), None

    if name == "desktop":
        goal = (args or {}).get("goal", "").strip()
        if not goal:
            return "No goal was given.", None
        return await bridge.run_goal(goal), None

    if name == "look":
        from . import vision
        try:
            frame = await asyncio.to_thread(vision.grab_screen, int((args or {}).get("display", 0)))
        except vision.Blind:
            return ("BLIND: the screenshot came back empty because Mint does not have Screen "
                    "Recording permission. You cannot see the screen, so do NOT guess positions or "
                    "call click_at. Tell the user to allow Mint in System Settings > Privacy & "
                    "Security > Screen & System Audio Recording, then restart Mint."), None
        except Exception as error:  # capture can fail without Screen Recording permission
            return (
                f"Could not capture the screen: {error}. The user may need to grant "
                "Screen Recording permission to the terminal running Mint."
            ), None
        return ("The screenshot was just sent to you as a video frame. Find what the user wants; "
                "to click it, use click_at with 0-1000 coordinates of that screenshot."), frame

    handler = _SYNC.get(name)
    if handler is None:
        return f"There is no tool called '{name}'.", None
    try:
        return await asyncio.to_thread(handler, args or {}), None
    except KeyError as error:
        return f"The {name} tool needs a {error} argument.", None
    except Exception as error:
        log.exception("tool %s failed", name)
        return f"The {name} tool failed: {error}", None


# --- Skills, memories, app resolution and Electron unlock (extra_tools.py) ------
# Wrapped here rather than threaded through the code above, so the two can be
# worked on separately.

_core_tools = tools
_core_dispatch = dispatch


def tools() -> list[types.Tool]:  # noqa: F811
    from . import extra_tools
    result = _core_tools()
    for tool in result:
        if tool.function_declarations:
            extra_tools.patch_declarations(tool.function_declarations)
    return result + [types.Tool(function_declarations=extra_tools.declarations())]


async def dispatch(name: str, args: dict) -> tuple[str, dict | None]:  # noqa: F811
    from . import extra_tools
    return await extra_tools.wrap(_core_dispatch, name, args)


# --- Sub-agents (agents/orchestrator.py) ------------------------------------------
# Mint orchestrates named sub-agents; its tools for that are declared and
# dispatched here, one more layer on top of the above.

_tools_before_agents = tools


def tools() -> list[types.Tool]:  # noqa: F811
    from .agents import orchestrator
    return _tools_before_agents() + [types.Tool(function_declarations=orchestrator.declarations())]


def _agent_handlers():
    from .agents import orchestrator
    return orchestrator.HANDLERS


_SYNC.update({name: (lambda a, _n=name: _agent_handlers()[_n](a))
              for name in ("list_agents", "delegate_task", "delegate_tasks", "agent_status", "message_agent",
                           "answer_agent", "stop_agent", "create_agent")})


# --- Long tasks across apps (work_tools.py) -----------------------------------------
# Waiting for an app to finish, and showing a built site on localhost.

_tools_before_work = tools


def tools() -> list[types.Tool]:  # noqa: F811
    from . import work_tools
    return _tools_before_work() + [types.Tool(function_declarations=work_tools.declarations())]


def _work_handlers():
    from . import work_tools
    return work_tools.HANDLERS


_SYNC.update({name: (lambda a, _n=name: _work_handlers()[_n](a)) for name in ("wait_until_done", "preview_site")})


# --- Harness: files, web, browser, menus, targeted scrolling, AppleScript (harness_tools.py) --
# Also guards every call: no typing into password fields, and a warning when the
# model repeats itself or keeps failing.

_tools_before_harness = tools
_dispatch_before_harness = dispatch


def tools() -> list[types.Tool]:  # noqa: F811
    from . import harness_tools
    # `clipboard` supersedes the plain text read/write tools: offer one way, keep the old names working.
    earlier = _tools_before_harness()
    for tool in earlier:
        if tool.function_declarations:
            tool.function_declarations = [d for d in tool.function_declarations
                                          if d.name not in {"read_clipboard", "write_clipboard"}]
    return earlier + [types.Tool(function_declarations=harness_tools.declarations())]


async def dispatch(name: str, args: dict) -> tuple[str, dict | None]:  # noqa: F811
    from . import harness_tools
    return await harness_tools.guard(_dispatch_before_harness, name, args)


def _harness_handlers():
    from . import harness_tools
    return harness_tools.HANDLERS


_SYNC.update({name: (lambda a, _n=name: _harness_handlers()[_n](a))
              for name in ("read_file", "write_file", "find_files", "file_action", "web_search", "read_url",
                           "browser", "scroll_to", "menu", "wait_for_text", "run_applescript",
                           "screenshot", "clipboard", "quit_mint", "pointer")})
# Not offered to the model: how a target is found in the front window (diagnostics).
_SYNC["harness_probe"] = lambda a: _harness_handlers()["probe"](a)
_SYNC["harness_probe_attrs"] = lambda a: _harness_handlers()["probe_attrs"](a)
_SYNC["harness_probe_strip"] = lambda a: _harness_handlers()["probe_strip"](a)
_SYNC["harness_probe_menu"] = lambda a: _harness_handlers()["probe_menu"](a)
_SYNC["harness_probe_ocr"] = lambda a: _harness_handlers()["probe_ocr"](a)
_SCREEN_ACTIONS.update({"browser", "scroll_to", "menu", "wait_for_text", "screenshot", "pointer"})


# --- Hearing: words Mint mishears, taught by the user (hearing.py) ------------------

_tools_before_hearing = tools


def tools() -> list[types.Tool]:  # noqa: F811
    from . import hearing
    return _tools_before_hearing() + [types.Tool(function_declarations=hearing.declarations())]


def _hearing_handlers():
    from . import hearing
    return hearing.HANDLERS


_SYNC["fix_hearing"] = lambda a: _hearing_handlers()["fix_hearing"](a)
