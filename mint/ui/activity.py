"""What a tool call looks like on screen: a kind of animation, a short phrase,
and an icon - the app's own icon when an app is being opened."""

from __future__ import annotations

from urllib.parse import urlparse

# kind -> SF Symbol for the badge on the orb
SYMBOLS = {
    "open": "arrow.up.forward.app",
    "web": "globe",
    "click": "cursorarrow.click.2",
    "type": "keyboard",
    "read": "text.viewfinder",
    "look": "eye",
    "scroll": "arrow.up.and.down",
    "write": "doc.richtext",
    "pdf": "doc.text",
    "calendar": "calendar",
    "mail": "envelope",
    "timer": "timer",
    "sound": "speaker.wave.2",
    "system": "gearshape",
    "plan": "list.bullet.clipboard",
    "switch": "rectangle.on.rectangle",
    "think": "sparkles",
    "file": "doc.on.doc",
    "search": "magnifyingglass",
    "code": "curlybraces",
    "menu": "filemenu.and.selection",
    "shot": "camera.viewfinder",
    "clip": "doc.on.clipboard",
}

_KIND = {
    "open_app": "open", "open_folder": "open", "open_chrome": "open", "open_slack": "open",
    "run_routine": "open", "quit_app": "system",
    "open_url": "web",
    "desktop": "click", "click_text": "click", "click_at": "click", "press_key": "type",
    "type_text": "type", "write_clipboard": "type",
    "read_window": "read", "get_selected_text": "read", "read_clipboard": "read",
    "list_open": "read", "list_windows": "read", "frontmost_app": "read", "list_accounts": "read",
    "look": "look",
    "scroll": "scroll",
    "create_note": "write", "compose_email": "mail",
    "create_pdf": "pdf", "export_doc_pdf": "pdf",
    "calendar_events": "calendar", "create_reminder": "calendar",
    "list_emails": "mail", "read_email": "mail",
    "set_timer": "timer",
    "set_volume": "sound", "media_key": "sound",
    "system_action": "system", "get_status": "system", "notify": "system",
    "plan_task": "plan", "step_done": "plan", "task": "plan",
    "switch_to": "switch",
    "wait_until_done": "timer", "preview_site": "web",
    "read_file": "file", "write_file": "file", "find_files": "file", "file_action": "file",
    "web_search": "search", "read_url": "search",
    "browser": "web", "scroll_to": "scroll", "menu": "menu", "wait_for_text": "timer",
    "run_applescript": "code",
    "screenshot": "shot", "clipboard": "clip", "quit_mint": "system", "read_clipboard": "clip", "write_clipboard": "clip",
    "pointer": "click",
    "watch_video": "look", "automation": "timer", "recall_history": "search",
    "edit_selection": "write", "make_spreadsheet": "file", "tidy": "file", "memory_used": "search",
    "teach": "look", "tutor": "look",
}

# Apps opened by a dedicated tool, by bundle id, for their icons.
_BUNDLES = {
    "open_chrome": "com.google.Chrome",
    "open_slack": "com.tinyspeck.slackmacgap",
    "open_url": "com.google.Chrome",
    "compose_email": "com.apple.mail", "list_emails": "com.apple.mail", "read_email": "com.apple.mail",
    "calendar_events": "com.apple.iCal", "create_reminder": "com.apple.reminders",
    "create_note": "com.apple.Notes",
    "open_folder": "com.apple.finder",
}


def kind(name: str) -> str:
    return _KIND.get(name, "think")


def _site(url: str) -> str:
    url = url.strip()
    host = urlparse(url if "//" in url else "https://" + url).netloc or url
    return host.removeprefix("www.")[:40]


def _quote(text, limit=48) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def phrase(name: str, args: dict) -> str:
    """'Opening Slack · oncall-support' rather than 'open slack · …'."""
    a = args or {}
    match name:
        case "open_app":
            return f"Opening {_quote(a.get('name', 'an app'))}"
        case "open_folder":
            return f"Opening {_quote(a.get('name', 'a folder'))}"
        case "open_url":
            return f"Going to {_site(str(a.get('url', '')))}"
        case "open_chrome":
            where = _site(str(a["url"])) if a.get("url") else "Chrome"
            return f"Opening {where}" + (f" · {_quote(a['account'], 28)}" if a.get("account") else "")
        case "open_slack":
            return "Opening Slack" + (f" · #{_quote(a['channel'], 28)}" if a.get("channel") else "")
        case "run_routine":
            return f"Running “{_quote(a.get('name', ''))}”"
        case "desktop":
            return _quote(str(a.get("goal", "Working on the screen")).capitalize(), 60)
        case "click_text":
            return f"Clicking “{_quote(a.get('text', ''), 40)}”"
        case "click_at":
            return "Clicking"
        case "press_key":
            mods = "+".join(a.get("modifiers") or [])
            return f"Pressing {mods + '+' if mods else ''}{a.get('key', '')}"
        case "type_text":
            return f"Typing “{_quote(a.get('text', ''), 40)}”"
        case "read_window":
            return "Reading the window"
        case "get_selected_text":
            return "Reading your selection"
        case "look":
            return "Looking at the screen"
        case "scroll":
            return f"Scrolling {a.get('direction', 'down')}"
        case "calendar_events":
            return "Checking your calendar"
        case "create_reminder":
            return f"Adding reminder · {_quote(a.get('title', ''), 36)}"
        case "create_note":
            return f"Writing a note · {_quote(a.get('title', ''), 36)}"
        case "compose_email":
            return "Drafting an email" + (f" to {_quote(a['to'], 30)}" if a.get("to") else "")
        case "list_emails":
            return "Checking your mail"
        case "read_email":
            return "Reading an email"
        case "create_pdf":
            return f"Making a PDF · {_quote(a.get('title', ''), 36)}"
        case "export_doc_pdf":
            return "Exporting the doc as PDF"
        case "set_volume":
            return f"Volume {a.get('level', '')}"
        case "media_key":
            return f"Media · {a.get('action', '')}"
        case "switch_to":
            return f"Switching to {_quote(a.get('what', ''), 40)}"
        case "list_open":
            return "Checking what's open"
        case "set_timer":
            return f"Timer · {a.get('minutes', '')} min"
        case "system_action":
            return f"System · {a.get('action', '')}"
        case "read_file":
            return f"Reading {_quote(str(a.get('path', 'a file')).rsplit('/', 1)[-1], 40)}"
        case "write_file":
            return f"Saving {_quote(str(a.get('path', 'a file')).rsplit('/', 1)[-1], 40)}"
        case "find_files":
            return f"Finding “{_quote(a['query'], 36)}”" if a.get("query") else "Looking through files"
        case "file_action":
            return f"{str(a.get('action', 'open')).replace('_', ' ').capitalize()} · " \
                   f"{_quote(str(a.get('path', '')).rsplit('/', 1)[-1], 34)}"
        case "web_search":
            return f"Searching · {_quote(a.get('query', ''), 40)}"
        case "edit_selection":
            return "Undoing the edit" if a.get("action") == "undo" else f"Rewriting · {_quote(a.get('instruction', ''), 34)}"
        case "make_spreadsheet":
            return f"Spreadsheet · {_quote(a.get('what') or a.get('source', ''), 34)}"
        case "tidy":
            return {"apply": "Tidying the folder", "undo": "Putting files back", "cancel": "Dropping the plan"}.get(
                str(a.get("action")), f"Planning a tidy-up · {_quote(a.get('folder') or 'Downloads', 30)}")
        case "tutor":
            return {"start": f"Showing you how · {_quote(a.get('task', ''), 30)}", "next": "Next step",
                    "back": "Previous step", "stop": "Ending the lesson"}.get(str(a.get("action")), "Lesson")
        case "teach":
            return {"start": "Watching how you do it", "stop": "Writing up what you showed me",
                    "cancel": "Stopped watching"}.get(str(a.get("action")), "Teaching")
        case "memory_used":
            return "Checking what I remembered"
        case "recall_history":
            return f"Looking back · {_quote(a.get('when') or a.get('question', ''), 34)}"
        case "automation":
            act = str(a.get("action", "list"))
            return {"create": f"New automation · {_quote(a.get('name') or a.get('do', ''), 30)}",
                    "list": "Checking automations"}.get(act, f"{act.replace('_', ' ').capitalize()} · "
                                                             f"{_quote(a.get('name', ''), 30)}")
        case "watch_video":
            src = str(a.get("source") or "")
            what = _site(src) if src.startswith("http") else (src.rsplit("/", 1)[-1] or "the video")
            return (f"Frame at {a['at']}" if a.get("at") else
                    f"Watching {_quote(what, 34)}" if not a.get("question") else f"About the video · {_quote(a['question'], 30)}")
        case "read_url":
            return f"Reading {_site(str(a.get('url', '')))}"
        case "browser":
            act = str(a.get("action", "read"))
            what = a.get("target") or ""
            return {"read": "Reading the page", "links": "Reading the links", "url": "Checking the page",
                    "js": "Running page script", "back": "Going back", "forward": "Going forward",
                    "reload": "Reloading", "tabs": "Checking the tabs", "wait": "Waiting for the page",
                    "go": f"Going to {_site(str(what))}", "new_tab": f"New tab · {_site(str(what))}",
                    "switch": f"Switching to {_quote(what, 30)}", "close_tab": "Closing a tab",
                    "group": f"Tab group · {_quote(what, 30)}", "rule": "Browser rules", "ungroup": "Ungrouping tabs",
                    "toolbar": f"Pressing {_quote(what, 30)}"
                    }.get(act) or f"{act.capitalize()} · {_quote(what, 36)}"
        case "scroll_to":
            return f"Scrolling to “{_quote(a['target'], 36)}”" if a.get("target") else \
                f"Scrolling the {_quote(a.get('where', 'pane'), 30)}"
        case "menu":
            return ("Menus · " if a.get("action") == "list" else "Menu · ") + _quote(a.get("path", ""), 44)
        case "wait_for_text":
            return f"Waiting for “{_quote(a.get('text', ''), 36)}”"
        case "run_applescript":
            return "Running AppleScript"
        case "screenshot":
            what = str(a.get("what", "screen"))
            return "Screenshot · " + ({"element": _quote(a.get("target") or "the picture", 30), "window": "window",
                                       "region": "region", "select": "your selection"}.get(what, "screen"))
        case "clipboard":
            return {"get": "Checking the clipboard", "copy": "Copying", "copy_file": "Copying a file",
                    "copy_image": "Copying a picture", "copy_path": "Copying the path",
                    "copy_selection": "Copying the selection", "paste": "Pasting", "history": "Clipboard history",
                    "restore": "Restoring a copy", "clear": "Clearing the clipboard"}.get(str(a.get("action")), "Clipboard")
        case "pointer":
            return {"where": "Looking at your pointer", "click": "Clicking at your pointer",
                    "double_click": "Double-clicking at your pointer",
                    "right_click": "Right-clicking at your pointer"}.get(str(a.get("action", "where")), "Pointer")
        case "plan_task":
            return f"Planning · {_quote(a.get('goal', ''), 44)}"
    return name.replace("_", " ").capitalize()


def app_hint(name: str, args: dict) -> tuple[str, str] | None:
    """Which app icon to show: ('bundle', id) or ('name', app name) or ('path', folder)."""
    if name == "open_app" and args.get("name"):
        return ("name", str(args["name"]))
    if name == "quit_app" and args.get("name"):
        return ("name", str(args["name"]))
    if name == "switch_to" and args.get("what"):
        return ("name", str(args["what"]))
    if name in _BUNDLES:
        return ("bundle", _BUNDLES[name])
    return None
