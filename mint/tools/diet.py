"""Fewer tools per voice session: the everyday ones declared, the rest found on demand.

Every Live session used to declare ~140 tools and ~40 prompt sections (~41k tokens) before the user said a
word. Most are rarely used. Here the session declares a fixed core set (from what is actually used, plus
what must be instant: opening, typing, timers, volume, stop) and two bridge tools:

    find_tools(query)       names, descriptions and argument schemas of matching hidden tools, with the
                            how-to guidance of their family (moved out of the always-on prompt)
    use_tool(name, args)    runs one of them. The session unwraps it to the real name BEFORE anything
                            else (guard, telegram gate, the trace, undo, the screen lease), so a hidden
                            tool goes through exactly the path of a declared one.

The list is decided once per Mint run and never changes inside a session (pref tool_diet False = every
tool declared, as before). Also here: big tool results are cut to head + tail for the model, with the whole
text saved to a file it can read_file.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import threading
import time
from pathlib import Path

from google.genai import types

log = logging.getLogger("mint.tools.diet")

# Declared in every voice session (9 Oct: ~3k tokens with the instruction). Only what a turn needs at once: opening,
# the mouse and keyboard, reading the window, the background job, the plan, stopping. Everything else is one
# find_tools away, picked for the request by router.py (hermes-agent's measured lesson: tools that must fire
# mid-flow - stop, the plan - stay declared).
CORE = frozenset({
    "open_app", "open_url", "switch_to", "type_text", "press_key", "scroll", "media_key", "set_volume",
    "ui_act", "click_text", "click_at", "look", "read_window",
    "get_status", "recall", "background_task", "plan_task", "step_done", "stop_listening",
})
BRIDGE = ("find_tools", "use_tool")

# Every tool not in CORE, by family: a short label (the prompt lists the labels), words people use for them, and
# the how-to sections that explain them - modules' PROMPT, or "module.NAME" (guides.py holds the chunks of the old
# instruction). find_tools and the request's context pack serve them; router.py picks the families per request.
FAMILIES: dict[str, dict] = {
    "apps, windows, clicking and checking what happened": {
        "label": "clicking and app windows",
        "tools": ("ui_elements", "drag", "scroll_to", "menu", "list_open", "quit_app", "open_folder", "desktop",
                  "verify_state", "wait_until_done"),
        "words": "click type button field menu dialog popup window app open folder finder quit close drag drop "
                 "scroll list controls elements check verify done wait finished loading search box sidebar tab "
                 "telegram whatsapp slack chatgpt claude vs code electron canvas",
        "prompts": ("guides.CLICKING",)},
    "chat apps: finding and opening a chat in Telegram, WhatsApp, Slack, Discord, Messages": {
        "label": "chat apps",
        "tools": (),
        "words": "chat message messages telegram whatsapp slack discord signal bot botfather dm channel group "
                 "conversation contact send reply",
        "prompts": ("guides.CHAT_APPS",)},
    "the web: searching, reading pages, browser tabs, tasks on websites, Chrome accounts": {
        "label": "web, browser and websites",
        "tools": ("web_search", "read_url", "browser", "web_goal", "open_chrome"),
        "words": "web search google look up online internet news price weather site website page url link "
                 "browser chrome brave safari tab tabs gmail account profile form book flights find out who what "
                 "when latest research",
        "prompts": ("guides.WEB",)},
    "files and folders": {
        "label": "files",
        "tools": ("read_file", "write_file", "find_files", "file_action", "run_applescript"),
        "words": "file files folder document pdf read write save find locate move rename copy trash delete "
                 "open reveal downloads desktop documents text markdown csv modified yesterday week applescript",
        "prompts": ("guides.FILES", "extra_tools.FILE_CARE")},
    "screenshots and the clipboard": {
        "label": "screenshots and clipboard",
        "tools": ("screenshot", "clipboard", "get_selected_text"),
        "words": "screenshot take screen shot capture picture window copy paste clipboard copied history pin "
                 "secret key password path selected selection highlighted this",
        "prompts": ("clip_tools",)},
    "reminders, calendar, notes, email drafts and timers": {
        "label": "reminders, calendar, notes, email, timers",
        "tools": ("set_timer", "create_reminder", "calendar_events", "create_event", "create_note",
                  "compose_email", "list_emails"),
        "words": "remind reminder timer minutes alarm calendar event meeting schedule today tomorrow note notes "
                 "email mail draft write inbox appointment",
        "prompts": ("guides.DAY",)},
    "music and the Mac's switches: brightness, dark mode, Do Not Disturb, window layouts, Wi-Fi": {
        "label": "music and Mac switches",
        "hint": "the Mac's own settings and music, not Mint's look",
        "tools": ("music", "mac", "system_action"),
        "words": "music song play pause spotify playlist artist album next skip brightness dark mode lock sleep "
                 "mute focus do not disturb window layout half maximize split night shift wifi keep awake "
                 "mission control settings display",
        "prompts": ("music", "macctl")},
    "undo and arithmetic": {
        "label": "undo, calculate",
        "tools": ("undo", "calculate"),
        "words": "undo put back revert redo calculate total sum add average percent math plus minus times",
        "prompts": ("undo", "calc")},
    "memory: remembering, changing and forgetting facts, and what happened before": {
        "label": "remembering and the past",
        "tools": ("remember", "update_memory", "forget", "recall_history"),
        "words": "remember forget memory memories know about me my name wife manager prefer changed wrong "
                 "yesterday last week earlier before history what did we",
        "prompts": ("guides.MEMORY", "journal")},
    "multi-step tasks: changing the plan, pausing, resuming": {
        "label": "changing a plan",
        "tools": ("task",),
        "words": "task plan step steps continue resume pause where were we abandon replan add",
        "prompts": ("guides.TASKS", "tasks")},
    "background jobs: status, changes, answers, stopping": {
        "label": "background jobs",
        "tools": ("agent_status", "answer_agent", "message_agent", "stop_agent"),
        "words": "job jobs background status progress running stop cancel change tell answer task",
        "prompts": ("guides.JOBS",)},
    "video editing and watching videos": {
        "label": "videos",
        "tools": ("edit_video", "video_info", "watch_video", "download_video"),
        "words": "video clip movie mp4 mov trim cut join merge speed slow captions subtitles gif reels vertical "
                 "compress mute audio extract rotate export youtube watch summarise transcript frame download "
                 "save grab vimeo instagram tiktok reel embedded stream m3u8 hls",
        "prompts": ("video_edit", "video", "video_download")},
    "documents, PDFs, OCR and conversions": {
        "label": "documents and PDFs",
        "tools": ("convert_document", "ocr_copy", "data_to_sheet", "create_pdf", "export_doc_pdf"),
        "words": "convert conversion pdf word docx document markdown html epub txt ocr scan text image copy "
                 "table data excel translate file language hindi export google doc",
        "prompts": ("convert",)},
    "spreadsheets": {
        "label": "spreadsheets",
        "tools": ("make_spreadsheet", "edit_spreadsheet"),
        "words": "spreadsheet sheet excel xlsx csv numbers table rows columns cell formula total sum invoices "
                 "receipts budget",
        "prompts": ("sheets",)},
    "translation": {
        "label": "translation",
        "tools": ("translate_screen",),
        "words": "translate translation language foreign page window say english hindi spanish french german",
        "prompts": ("translate",)},
    "image generation": {
        "label": "images",
        "tools": ("make_image",),
        "words": "image picture photo draw drawing generate art sketch illustration logo wallpaper playground",
        "prompts": ("imagegen",)},
    "screen recording and finding screenshots": {
        "label": "screen recording",
        "tools": ("screen_record", "find_screenshot"),
        "words": "record recording screen video capture area screenshots find old earlier",
        "prompts": ("screenrec", "screenshots")},
    "Apple apps: Notes, Reminders lists, calendar changes, Contacts, Maps, Safari, Photos, Pages/Keynote/Numbers": {
        "label": "Apple apps",
        "tools": ("notes", "reminders_manage", "calendar_manage", "contacts", "maps", "safari", "photos", "iwork"),
        "words": "note notes append grocery list reminders due overdue complete done reschedule calendar move "
                 "event rename contact phone number email birthday address maps directions route eta drive "
                 "safari reading list bookmarks photos album pictures pages keynote numbers presentation slides",
        "prompts": ("apple_apps",)},
    "tidying and merging folders": {
        "label": "tidying folders",
        "tools": ("tidy", "merge_folders"),
        "words": "tidy clean organise organize sort folder downloads desktop merge folders duplicates",
        "prompts": ("tidy", "merge")},
    "meetings and Google Meet calls": {
        "label": "meetings",
        "tools": ("meeting", "google_meet"),
        "words": "meeting call record notes transcript zoom teams huddle facetime google meet join share screen "
                 "action items standup",
        "prompts": ("meetings", "meet_call")},
    "automations, routines and Shortcuts": {
        "label": "automations and Shortcuts",
        "tools": ("automation", "run_routine", "shortcut", "make_shortcut", "pause_everything"),
        "words": "automation schedule every day daily morning trigger when routine shortcut shortcuts siri "
                 "recurring later at pause resume everything all jobs",
        "prompts": ("automations", "apple_shortcuts", "shortcut_maker")},
    "teaching Mint and tutoring": {
        "label": "teaching and tutoring",
        "tools": ("teach", "tutor"),
        "words": "teach watch me learn show you how tutor explain lesson practice quiz homework",
        "prompts": ("teach", "tutor")},
    "daily briefing": {
        "label": "briefing",
        "tools": ("briefing",),
        "words": "briefing brief morning summary day news agenda today",
        "prompts": ("briefing",)},
    "email triage": {
        "label": "email triage",
        "tools": ("mail", "read_email"),
        "words": "mail email inbox triage unread important reply draft archive read",
        "prompts": ("mailtriage",)},
    "trackers: tell me when something finishes": {
        "label": "trackers",
        "tools": ("track",),
        "words": "track tracker tell notify ping let me know when finishes done download build upload export",
        "prompts": ("trackers",)},
    "notifications": {
        "label": "notifications",
        "tools": ("notifications", "notify"),
        "words": "notifications notification center missed dismiss clear reply alert banner",
        "prompts": ("notifications",)},
    "dictation": {
        "label": "dictation",
        "tools": ("dictation",),
        "words": "dictation dictate type what i say voice typing",
        "prompts": ("dictation",)},
    "connectors to apps and services": {
        "label": "connectors",
        "tools": ("connector",),
        "words": "connector connect integration integrate service app notion things setup status",
        "prompts": ("connector_maker",)},
    "delegating to sub-agents (Astra, Luna, Sage, Codex)": {
        "label": "sub-agents",
        "tools": ("delegate_task",),
        "words": "agent agents astra luna sage codex delegate ask give hand research build write sub-agent team",
        "prompts": ("orchestrator.prompt_text", "orchestrator")},    # the roster (live) and the rules
    "orb expressions (smile, dance, wave)": {
        "label": "orb expressions",
        "tools": ("express",),
        "words": "express expression smile laugh love heart blush cry angry surprised sleepy dizzy cool thinking "
                 "wink kiss wave clap praise dance show party perform emotion face",
        "prompts": ()},                 # its rules are in its own description, so use_tool runs it at once
    "the ChatGPT and Claude desktop apps": {
        "label": "ChatGPT and Claude apps",
        "tools": ("agent_app",),
        "words": "chatgpt claude desktop app ask tell send prompt reply answer read latest chat chats project "
                 "projects session sessions busy doing writing stop new",
        "prompts": ("agentapps",)},
    "coding agents and sub-agents": {
        "label": "coding agents",
        "tools": ("claude_mode", "delegate_tasks", "create_agent", "list_agents"),
        "words": "claude code codex agent agents coding session terminal delegate sub-agent job jobs stop "
                 "message tell tests test passed failing green broke usage limits left standup recap handoff "
                 "hand off continue",
        "prompts": ("notch_agents",)},
    "handing files to an app": {
        "label": "files to an app",
        "tools": ("hand_to_app",),
        "words": "open with keka zip compress unzip extract unarchiver preview drop drag into app files",
        "prompts": ("handoff",)},
    "your settings, orb, notch, voice, cards and showing things on screen": {
        "label": "your settings and orb",
        "hint": "how Mint itself looks, sounds and behaves: its colour theme ('make it pink'), where it sits, chat "
                "only or speaking, the mic, reply language, its voice; pointing things out on screen",
        "tools": ("set_preference", "show_on_screen", "mark_area", "clear_marks", "move_orb", "display_mode",
                  "notch_files", "screen_share_visibility", "set_voice", "show_card"),
        "words": "show point highlight underline circle box arrow mark where orb move up down left right "
                 "circle notch island dynamic orb mode voice deeper cheerful accent card count list tiles "
                 "screen share hide visible speak talk chat mic microphone theme color pink purple position "
                 "language reply hindi setting preference spoken",
        "prompts": ("guides.SETTINGS", "extra_tools.SHOWING", "extra_tools.EXPRESSIVE", "cards")},
    "rewriting selected text": {
        "label": "rewriting text",
        "tools": ("edit_selection",),
        "words": "selected selection rewrite formal grammar shorten bullets polish rephrase",
        "prompts": ("rewrite",)},
    "skills: learned how-tos, and memory admin": {
        "label": "skills (how-tos)",
        "tools": ("find_skill", "skill_result", "create_skill", "update_skill", "list_skills", "delete_skill",
                  "skill_history", "learn_skill", "list_memories", "memory_used", "show_skills_and_memory"),
        "words": "skill skills save how to learned memory memories brain history learn this page clipboard teach "
                 "next time steps",
        "prompts": ("guides.SKILLS",)},
    "hearing fixes": {
        "label": "hearing fixes",
        "tools": ("fix_hearing",),
        "words": "mishear misheard hearing word wrong pronounce",
        "prompts": ()},
    "your own setup: what is set up, connecting accounts, keys and permissions": {
        "label": "your setup",
        "tools": ("setup_status", "open_setup"),
        "words": "setup set connect connected telegram gmail google account meet openai gemini typesafe key keys "
                 "permission permissions access accessibility microphone calendar shortcuts claude hooks voice "
                 "train trained capabilities features what can you do settings",
        "prompts": ("setup_guide",)},
    "Mac and Mint odds and ends": {
        "label": "odds and ends",
        "tools": ("frontmost_app", "list_windows", "open_slack", "list_accounts", "chat_action", "show_chat",
                  "quit_mint", "update", "pointer", "wait_for_text", "preview_site"),
        "words": "front window windows slack workspace channel accounts chat clear summarize new session quit "
                 "update version mouse pointer wait text appear localhost preview site",
        "prompts": ()},
}

# Sections whose content guides.py now carries: dropped from the voice prompt even though no family serves them.
REPLACED = ("extra_tools.PROMPT", "harness_tools")

# Short descriptions for the declared tools (the full ones stay in their modules for the background worker and
# find_tools(<the tool's name>)).
LIVE: dict[str, str] = {
    "open_app": "Open or switch to a Mac app by name.",
    "open_url": "Open a web address in a new tab; the browser is picked for you.",
    "switch_to": "Bring an open app, Chrome tab or window to the front by words from its title.",
    "type_text": "Type text at the cursor in the focused field of any app.",
    "press_key": "Press a key, with modifiers.",
    "scroll": "Scroll the window under the pointer.",
    "media_key": "Play, pause, next or previous track (Spotify / Music told directly).",
    "set_volume": "Set the Mac's volume.",
    "ui_act": "Click, type into or pick a control in an app's window, described in plain words; reports what "
              "changed. One control per call.",
    "click_text": "Click text you can see on screen, when ui_act can't find the control.",
    "click_at": "Last resort: click something in the latest look, named by target; x, y (0-1000) are hints.",
    "look": "See the screen: images, charts, layouts, why an action didn't work. Not routinely.",
    "read_window": "Read ALL the text in the front window exactly, off-screen parts too.",
    "get_status": "The local time and date, battery and volume.",
    "recall": "Look up saved memories about the user (instant) before answering anything personal.",
    "background_task": "Run a job needing several tool calls in the BACKGROUND (research, drafts, files, apps on "
                       "screen) while you keep talking. Write the whole job.",
    "plan_task": "Start a task of three or more steps: the goal and ordered steps first.",
    "step_done": "Mark a plan step finished (or failed) with what you checked.",
    "stop_listening": "Go back to sleep when the user is done. Say a short goodbye first.",
}
LIVE_ARGS: dict[str, dict[str, str]] = {
    "open_app": {"name": "The app's name."},
    "open_url": {"browser": "Only when the user named one."},
    "type_text": {"press_return": "Press Return after; never to send a message unless asked.",
                  "field": "A box to type into if not the focused one ('search box')."},
    "press_key": {"key": "return, escape, tab, space, arrows, a letter or digit."},
    "scroll": {"amount": "Steps; about 5 is one screen."},
    "ui_act": {"action": "dismiss closes the open dialog, menu or popup.",
               "target": "The control in plain words: 'Create project button in the dialog'.",
               "text": "For type: what to type.", "press_return": "For type: press Return after.",
               "app": "Optional: its app if not the front one."},
    "click_text": {"text": "The label as it appears.", "app": "Optional: its app if not the front one."},
    "click_at": {"target": "Its visible text or a short description.", "x": "0-1000 from the left.",
                 "y": "0-1000 from the top.", "button": "left or right."},
    "look": {"display": "0 = all, 1 = main."},
    "read_window": {"max_chars": "Default 12000."},
    "recall": {"question": "What you need to know.", "deep": "Also search archived memories."},
    "background_task": {"task": "The whole job, in full sentences.", "title": "3-6 words for it.",
                        "context": "What it needs from the conversation."},
    "plan_task": {"steps": "Short steps in order."},
    "step_done": {"step": "'2', or '3.1'.", "result": "What you checked and saw."},
    "set_volume": {"level": "0 to 100."},
    "media_key": {"action": "play, pause, next or previous (playpause only to toggle)."},
}

BUDGET = 6000              # chars one find_tools answer may use
GUIDE_MAX = 3000           # of which a family's how-to guidance
RESULT_CAP = 20000         # a tool result longer than this is cut for the model ...
RESULT_HEAD, RESULT_TAIL = 14000, 4000      # ... to its start and end; the whole text goes to a file
SPILL_DIR = Path.home() / "Library" / "Logs" / "Mint" / "results"
SPILL_KEEP = 30
NEVER_CUT = {"read_file", "find_tools", "look", "screenshot"}   # read_file pages itself (and reads the spills)

_lock = threading.Lock()
_on: bool | None = None            # decided once per run: the tool list never changes inside a session
_catalog: dict[str, types.FunctionDeclaration] = {}     # every declared tool by name (full list)
_guides: dict[str, str] = {}                            # family -> its how-to text
_served: set[str] = set()          # families whose how-to the model has been given in this session
_spills = 0


def enabled() -> bool:
    global _on
    with _lock:
        if _on is None:
            try:
                from mint.core import prefs
                _on = bool(prefs.get("tool_diet") if prefs.get("tool_diet") is not None else True)
            except Exception:
                _on = True
        return _on


def family_of(name: str) -> str:
    for family, info in FAMILIES.items():
        if name in info["tools"]:
            return family
    return ""                          # a new tool not given a family yet: still found by its name and words


# --- the declared list --------------------------------------------------------------------------

def declarations() -> list[types.FunctionDeclaration]:
    S = types.Type.STRING
    return [
        types.FunctionDeclaration(
            name="find_tools",
            description=("The tools and how-to for a request beyond your own tools: the request in a few words "
                         "('trim a video', 'add a reminder'), or a tool's name. Then use_tool."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "query": types.Schema(type=S, description="The job in plain words."),
            }, required=["query"])),
        types.FunctionDeclaration(
            name="use_tool",
            description="Run a tool find_tools showed you, or one your instructions name.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "name": types.Schema(type=S, description="The tool's exact name."),
                "args": types.Schema(type=S, description="Its arguments as a JSON object; {} for none."),
            }, required=["name"])),
    ]


def live_tools(full: list[types.Tool]) -> list[types.Tool]:
    """What a voice session declares: the core tools and the bridge, or everything with the diet off.
    Also remembers the full list, for find_tools and use_tool."""
    remember(full)
    _served.clear()                    # a new session: the model has read nothing yet
    _shown.clear()
    _kits.clear()
    if not enabled():
        return full
    hidden = {n for n in _catalog if n not in CORE}
    kept = []
    for tool in full:
        decls = [_with_hints(_short(d), hidden) for d in tool.function_declarations or [] if d.name in CORE]
        if decls:
            kept.append(types.Tool(function_declarations=decls))
        elif not tool.function_declarations:
            kept.append(tool)           # a non-function tool (none today) passes through
    return kept + [types.Tool(function_declarations=declarations())]


def remember(full: list[types.Tool]) -> None:
    found = {}
    for tool in full:
        for decl in tool.function_declarations or []:
            found.setdefault(decl.name, decl)
    with _lock:
        _catalog.clear()
        _catalog.update(found)


def _all() -> dict[str, types.FunctionDeclaration]:
    if not _catalog:
        from mint.tools import registry as tools
        remember(tools.tools())
    return _catalog


def _mention(name: str) -> str:
    """How a description names a tool: tool_names always; plain words (notes, mail, track) only as a tool."""
    n = re.escape(name)
    if "_" in name:
        return rf"\b{n}\b"
    return rf"`{n}`|\b(?:use|call|the) {n} (?:tool|action)|\b{n} (?:tool\b|action=)|\b{n}\("


def _arg(schema, path: str):
    """The argument schema at 'name' or 'name.inner' (inside an array's items), or None."""
    for part in path.split("."):
        if schema is not None and schema.items is not None and not schema.properties:
            schema = schema.items
        schema = (schema.properties or {}).get(part) if schema is not None else None
    return schema


def _short(decl: types.FunctionDeclaration) -> types.FunctionDeclaration:
    """A declared tool with its short live description and argument notes (LIVE, LIVE_ARGS)."""
    if decl.name not in LIVE and decl.name not in LIVE_ARGS:
        return decl
    copy = decl.model_copy(deep=True)
    copy.description = LIVE.get(decl.name, copy.description)
    for path, text in LIVE_ARGS.get(decl.name, {}).items():
        if (found := _arg(copy.parameters, path)) is not None:
            found.description = text
    return copy


def _with_hints(decl: types.FunctionDeclaration, hidden: set[str]) -> types.FunctionDeclaration:
    """A declared tool whose description sends the model to a hidden one says how to reach it."""
    text = decl.description or ""
    named = [n for n in hidden if re.search(_mention(n), text)]
    if not named:
        return decl
    copy = decl.model_copy(deep=True)
    copy.description = text + f" ({', '.join(sorted(named))}: through use_tool.)"
    return copy


# --- the prompt -------------------------------------------------------------------------------------

def clip(text: str, limit: int, more: str = "") -> str:
    """`text` within `limit` characters, cut at a line end where it can be: the parts of the voice prompt that grow
    with the user's data (memory notes, accounts, history) keep the fixed per-step prompt small (9 Oct: under ~5k
    tokens even for a heavy user; the rest is one recall / find_tools away)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = cut.rfind("\n")
    if end > limit * 0.6:
        cut = cut[:end]
    return cut.rstrip() + " …" + (f" ({more})" if more else "")



def _sources() -> dict:
    """The modules whose prompt sections the families carry (imported here so the export can move them)."""
    from mint.tools import agentapps
    from mint.tools import apple_apps
    from mint.tools import shortcuts as apple_shortcuts
    from mint.tools import automations
    from mint.tools import briefing
    from mint.tools import calc
    from mint.tools import cards
    from mint.tools import clipboard as clip_tools
    from mint.tools import connector_maker
    from mint.tools import convert
    from mint.voice import dictation
    from mint.tools import extra as extra_tools
    from mint.core import guides
    from mint.tools import handoff
    from mint.tools import harness as harness_tools
    from mint.tools import imagegen
    from mint.knowledge import journal
    from mint.tools import mac as macctl
    from mint.tools import mailtriage
    from mint.app import meet_call
    from mint.tools import meetings
    from mint.tools import merge
    from mint.tools import music
    from mint.ui import notch_agents
    from mint.tools import notifications
    from mint.tools import rewrite
    from mint.tools import screenrec
    from mint.tools import screenshots
    from mint.tools import setup_guide
    from mint.tools import sheets
    from mint.tools import shortcut_maker
    from mint.app import tasks
    from mint.knowledge import teach
    from mint.tools import tidy
    from mint.tools import trackers
    from mint.tools import translate
    from mint.ui import tutor
    from mint.tools import undo
    from mint.tools import video
    from mint.tools import video_download
    from mint.tools import video_edit
    from mint.agents import orchestrator
    found = dict(locals())
    return {name: module for name, module in found.items() if not name.startswith("_")}


def _section(source: str) -> str:
    """'video_edit' -> video_edit.PROMPT; 'extra_tools.SHOWING' -> that constant; 'orchestrator.prompt_text' ->
    what that function says now."""
    module, _, attr = source.partition(".")
    try:
        found = _sources().get(module)
        value = getattr(found, attr or "PROMPT", "") if found is not None else ""
        return str((value() if callable(value) else value) or "")
    except Exception:
        log.debug("no prompt section %s", source, exc_info=True)
        return ""


def _live_section(source: str) -> bool:
    module, _, attr = source.partition(".")
    return bool(attr) and callable(getattr(_sources().get(module), attr, None))


def guide(family: str) -> str:
    sources = FAMILIES.get(family, {}).get("prompts", ())
    if family in _guides:
        return _guides[family]
    text = "\n".join(p for p in (_section(s).strip() for s in sources) if p)
    if not any(_live_section(s) for s in sources):
        _guides[family] = text
    return text


def prompt_line() -> str:
    labels = "; ".join(info.get("label") or family for family, info in FAMILIES.items())
    return ("find_tools also covers: " + labels + ". A tool these instructions name that is not in your list: "
            "use_tool with its name.")


def prompt_parts(parts: list[str]) -> list[str]:
    """The prompt sections for a voice session: the families' own sections leave (find_tools serves them),
    and so do the ones guides.py now carries; one line says what find_tools brings."""
    if not enabled():
        return parts
    sources = [s for family in FAMILIES for s in FAMILIES[family]["prompts"] if not _live_section(s)]
    moved = {text for s in (*sources, *REPLACED) if (text := _section(s).strip())}
    return [p for p in parts if p.strip() not in moved] + [prompt_line()]


# --- find_tools ---------------------------------------------------------------------------------------

_STOP = set("a an the to of in on for and or my me i it this that with from by at be is are do does can "
            "you your please want need make some any all get".split())


def _words(text: str) -> list[str]:
    out = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " ")):
        if word in _STOP or len(word) < 2:
            continue
        for end in ("ing", "es", "s", "ed"):
            if len(word) > len(end) + 3 and word.endswith(end):
                word = word[: -len(end)]
                break
        out.append(word)
    return out


def _documents() -> dict[str, dict[str, list[str]]]:
    docs = {}
    for name, decl in _all().items():
        if name in CORE and enabled():
            continue
        family = family_of(name)
        info = FAMILIES.get(family, {})
        docs[name] = {"name": _words(name), "family": _words(f"{family} {info.get('words', '')}"),
                      "description": _words(decl.description or ""), "guide": _words(guide(family))}
    return docs


_WEIGHTS = {"name": 4.0, "family": 2.5, "description": 1.5, "guide": 0.6}


def rank(query: str, limit: int = 8) -> list[str]:
    """Hidden tools for `query`, best first (lexical: name, family words, description, guidance)."""
    docs = _documents()
    wanted = _words(query)
    if not wanted:
        return []
    exact = (query or "").strip().lower()
    total = len(docs) or 1
    scores = {}
    for name, fields in docs.items():
        score = 100.0 if name == exact else 0.0
        for word in set(wanted):
            have = sum(1 for f in docs.values() if any(word in v for v in f.values()))
            idf = math.log(1 + total / (1 + have))
            for field, weight in _WEIGHTS.items():
                if word in fields[field]:
                    score += weight * idf
        if score > 0:
            scores[name] = score
    best = sorted(scores, key=lambda n: -scores[n])
    if best:
        floor = scores[best[0]] * 0.25          # far behind the best match: noise
        best = [n for n in best if scores[n] >= floor]
    return best[:limit]


def _named(text: str) -> str:
    """Descriptions speak of the assistant by name: the one the user gave it (as the session does)."""
    try:
        from mint.core import prefs
        name = prefs.name()
    except Exception:
        name = "Mint"
    return text if name == "Mint" else text.replace("Mint", name)


def _schema(decl: types.FunctionDeclaration) -> str:
    if getattr(decl, "parameters_json_schema", None):
        schema = decl.parameters_json_schema
    else:
        from mint.app.background import _json_schema
        schema = _json_schema(decl.parameters)
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def find(query: str, budget: int = BUDGET) -> str:
    """The find_tools answer: matching hidden tools with their arguments, and their family's guidance,
    within `budget` characters."""
    query = " ".join(str(query or "").split())[:200]
    own = query.lower().strip("`'\" ")
    if enabled() and own in CORE and own in _all():
        # One of the declared tools by name: its full notes (the declared description is the short one, LIVE).
        decl = _all()[own]
        return (f"{own} is already one of your tools - call it directly, not through use_tool. Its full notes: "
                f"{_named(decl.description or '')}\n  args: {_schema(decl)}")[:budget]
    best = rank(query)
    core_hits = [n for n in CORE if n in _all() and (n == query.lower() or set(_words(n)) & set(_words(query))
                                                    and n.replace("_", " ") in query.lower())]
    asked = set(_words(query))         # declared tools of a family the query is about ("connect telegram": setup)
    core_hits += sorted(n for n in CORE if n in _all() and n not in core_hits and family_of(n)
                        and asked & set(_words(FAMILIES[family_of(n)].get("core_when")
                                               or FAMILIES[family_of(n)]["words"])))
    if not best:
        return (f"No hidden tool matches '{query}'. "
                + (f"You already have: {', '.join(core_hits)}. " if core_hits else "")
                + "The families: " + "; ".join(f"{f} ({', '.join(i['tools'])})" for f, i in FAMILIES.items())
                + ". Try other words, or do it with the tools you have.")[:budget]
    head = f"Tools for '{query}' - run one with use_tool(name, args=JSON object):"
    out, used = [head], len(head)
    first_family = family_of(best[0])
    # Room for the family's guidance is kept first: with long tool descriptions (the video tools, 8 Oct) the list
    # filled the budget, the guidance was left out, and use_tool then refused the first call to "read it first".
    guidance = guide(first_family)
    reserve = min(len(guidance), GUIDE_MAX) + 60 if guidance else 0
    shown, short = [], []
    for name in best:
        decl = _all()[name]
        entry = f"\n- {name}: {_named(decl.description or '')}\n  args: {_schema(decl)}"
        if used + len(entry) > budget - 200 - reserve and shown:
            short.append(name)
            continue
        out.append(entry[: max(200, budget - used - 200 - reserve)] if not shown else entry)
        used += len(out[-1])
        shown.append(name)
    if short:
        line = "\nAlso (find_tools with the name for its arguments): " + ", ".join(
            f"{n} ({(_all()[n].description or '').split('. ')[0][:70]})" for n in short)
        out.append(line)
        used += len(line)
    text = guidance
    if text and used < budget - 120:
        _served.add(first_family)
        room = min(GUIDE_MAX, budget - used - 40)
        out.append(f"\nHow to use them:\n{text[:room]}" + ("…" if len(text) > room else ""))
    if core_hits:
        out.append(f"\n(Already in your tools: {', '.join(core_hits)}.)")
    return "".join(out)[:budget]


# --- the request's toolkit (router.py picks the groups) --------------------------------------------------------

PACK_BUDGET = 6500
_shown: set[str] = set()           # tools whose arguments the model has been given in this session


def pack(groups: list[str], about: str = "", budget: int = PACK_BUDGET) -> str:
    """The tools and how-to of `groups` for one request, within `budget` characters: each group's how-to (once
    a session), then its tools - the ones `about` names first - with their arguments (once a session; later just
    named). What doesn't fit is named, for find_tools."""
    groups = [g for g in groups if g in FAMILIES]
    if not groups:
        return ""
    wanted = set(_words(about))
    head = "Picked for this request - run these with use_tool(name, args=JSON object):"
    out, used, left_out, known = [head], len(head), [], []
    for i, family in enumerate(groups):
        info = FAMILIES[family]
        share = (budget - used) // (len(groups) - i)
        spent = 0
        title = f"\n## {info.get('label') or family}"
        out.append(title)
        spent += len(title)
        text = guide(family)
        if text and family not in _served:
            room = min(GUIDE_MAX, max(300, share // 2))
            out.append("\n" + text[:room] + ("…" if len(text) > room else ""))
            spent += min(len(text), room) + 2
            _served.add(family)
        names = [n for n in info["tools"] if n in _all() and not (enabled() and n in CORE)]
        names.sort(key=lambda n: -len(wanted & set(_words(f"{n} {_all()[n].description or ''}"))))
        for name in names:
            if name in _shown:
                known.append(name)
                continue
            decl = _all()[name]
            entry = f"\n- {name}: {_named(decl.description or '')[:400]}\n  args: {_schema(decl)}"
            if spent + len(entry) > share:
                left_out.append(name)
                continue
            out.append(entry)
            spent += len(entry)
            _shown.add(name)
        used += spent
    if known:
        out.append(f"\n(Given earlier: {', '.join(known)}.)")
    if left_out:
        out.append(f"\nAlso: {', '.join(left_out)} (find_tools with the name for its arguments).")
    return "".join(out)


def find_for_request(query: str, request: str | None = None) -> str:
    """find_tools for the voice session: the groups router.py picked for the request in progress, plus the
    families of what the query itself names - their how-to and tools in one answer."""
    query = " ".join(str(query or "").split())[:200]
    if not enabled() or query.lower().strip("`'\" ") in _all():
        return find(query)                        # a tool by name: its full notes and arguments
    from mint.app import live
    from mint.tools import router
    request = live.request() if request is None else request
    groups = list(router.route(request).groups) if request else []
    for name in rank(query)[:3]:
        family = family_of(name)
        if family and family not in groups:
            groups.append(family)
    if not groups:
        return find(query)
    answer = pack(groups[:4], f"{query} {request}")
    if answer and request:
        _kits.add(_request_key(request))
    return answer or find(query)


_kits: set[str] = set()            # requests whose toolkit find_tools already handed over


def _request_key(request: str) -> str:
    return " ".join(str(request or "").lower().split())[:300]


def kit_given(request: str) -> bool:
    """find_tools already gave this request its toolkit: the first tool result need not repeat it (9 Oct live run:
    a second pack of the same groups' other tools cost ~1.2k tokens on every later step)."""
    return _request_key(request) in _kits


# --- use_tool -------------------------------------------------------------------------------------------

# The screen tools a misnamed call is taken for. Seen 9 Oct: Gemini Live called 'pressed_key' (cmd+F) four times in
# one Telegram request and got "There is no tool called" each time.
_ALIASABLE = ("press_key", "type_text", "click_text", "click_at", "ui_act", "ui_elements", "scroll", "look",
              "read_window", "open_app", "switch_to")


def _alias(name: str) -> str:
    if name in _ALIASABLE or not name:
        return name
    near = difflib.get_close_matches(name, _ALIASABLE, n=1, cutoff=0.85)
    if near and name not in _all():
        log.info("tool %s taken for %s", name, near[0])
        return near[0]
    return name


def unwrap(name: str, args: dict | None) -> tuple[str, dict, str]:
    """(the real tool, its arguments, '') for a use_tool call - or (name, args, why not). Any other call
    comes back as it is. The caller runs the real tool through its normal path."""
    args = dict(args or {})
    if name != "use_tool":
        return _alias(name), args, ""
    inner = " ".join(str(args.get("name") or args.get("tool") or "").split()).strip("`'\"")
    raw = args.get("args", args.get("arguments", {}))
    if not inner:
        return name, args, "use_tool needs `name`: the tool to run (find_tools lists them)."
    if inner in BRIDGE:
        return name, args, f"use_tool runs a tool; call {inner} directly."
    known = _all()
    if inner not in known:
        near = difflib.get_close_matches(inner, list(known), n=3, cutoff=0.6)
        return name, args, (f"There is no tool called '{inner}'."
                            + (f" Did you mean {' or '.join(near)}?" if near else "")
                            + " Call find_tools with the job in a few words.")
    if isinstance(raw, dict):
        inner_args = raw
    else:
        text = str(raw or "").strip()
        try:
            inner_args = json.loads(text) if text else {}
        except ValueError:
            return name, args, (f"NOT RUN: `args` for {inner} is not valid JSON. Pass a JSON object, e.g. "
                                f"{{\"key\": \"value\"}}. Its arguments: {_schema(known[inner])}")
        if not isinstance(inner_args, dict):
            return name, args, f"NOT RUN: `args` must be a JSON object. Arguments of {inner}: {_schema(known[inner])}"
    required = list(getattr(known[inner].parameters, "required", None) or [])
    missing = [r for r in required if inner_args.get(r) in (None, "")]
    if missing:
        return name, args, (f"NOT RUN: {inner} needs {', '.join(missing)}. Its arguments: "
                            f"{_schema(known[inner])}")
    family = family_of(inner)
    if enabled() and inner not in CORE and family not in _served and guide(family):
        # Called without find_tools: its rules (when to record, what never to send...) were never read.
        _served.add(family)
        return name, args, (f"NOT RUN yet - first read how {inner} is used, then call use_tool again with "
                            f"the right args:\n{guide(family)[:GUIDE_MAX]}\nIts arguments: {_schema(known[inner])}")
    return inner, inner_args, ""


# --- big results -------------------------------------------------------------------------------------------

def fit(name: str, result):
    """A result too big for the conversation: its start and end, and where the whole text is."""
    if not isinstance(result, str) or len(result) <= RESULT_CAP or name in NEVER_CUT:
        return result
    path = _spill(name, result)
    cut = len(result) - RESULT_HEAD - RESULT_TAIL
    where = (f"the whole result is saved in {path} - read_file it (start_line=...) for the part you need"
             if path else "the whole result could not be saved")
    return (f"{result[:RESULT_HEAD]}\n\n[... {cut} characters cut here to keep the conversation small; {where}. "
            "Don't repeat this call to see it.]\n\n" + result[-RESULT_TAIL:])


def _spill(name: str, text: str) -> str:
    try:
        SPILL_DIR.mkdir(parents=True, exist_ok=True)
        global _spills
        _spills += 1                   # two big results in one second must not overwrite each other
        path = SPILL_DIR / (f"{time.strftime('%Y%m%d-%H%M%S')}-{_spills:03d}-"
                            f"{re.sub(r'[^a-z0-9_]', '', name.lower())[:30]}.txt")
        path.write_text(text, encoding="utf-8")
        old = sorted(SPILL_DIR.glob("*.txt"), key=lambda p: (p.stat().st_mtime, p.name))
        for stale in old[:-SPILL_KEEP]:
            try:
                stale.unlink()
            except OSError:
                pass
        return str(path).replace(str(Path.home()), "~", 1)
    except OSError:
        log.warning("could not save a big %s result", name, exc_info=True)
        return ""


def reset() -> None:
    """For tests: decide the diet again and forget the catalog."""
    global _on
    with _lock:
        _on = None
        _catalog.clear()
    _guides.clear()
    _served.clear()
    _shown.clear()
    _kits.clear()

