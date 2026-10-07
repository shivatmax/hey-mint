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

# Declared in every voice session. Chosen from tools.log + history (calls over the last weeks) and from
# latency: things said in passing ("pause the music", "set a timer", "volume down") must not cost a lookup.
CORE = frozenset({
    # apps, windows, keys
    "open_app", "open_url", "open_folder", "open_chrome", "switch_to", "quit_app", "list_open",
    "type_text", "press_key", "scroll", "media_key", "set_volume", "clipboard", "get_selected_text",
    # the screen
    "look", "ui_act", "ui_elements", "click_text", "click_at", "read_window", "desktop", "scroll_to", "menu",
    "screenshot", "verify_state",
    # files and the web
    "read_file", "write_file", "find_files", "file_action", "web_search", "read_url", "browser", "web_goal", "run_applescript",
    # the day
    "get_status", "set_timer", "create_reminder", "calendar_events", "create_event", "create_note",
    "compose_email", "list_emails", "system_action", "mac", "music", "calculate", "undo",
    # the conversation itself
    "stop_listening", "set_preference", "express", "plan_task", "step_done", "task", "wait_until_done",
    # memory and skills
    "remember", "recall", "update_memory", "forget", "find_skill", "skill_result", "recall_history",
    # parallel work and agents
    "background_task", "agent_status", "answer_agent", "message_agent", "stop_agent", "delegate_task", "agent_app",
    "pause_everything",
})
BRIDGE = ("find_tools", "use_tool")

# The hidden tools by family: words people use for them, and the prompt sections that explain them (served by
# find_tools instead of riding along in every session). `prompts` are module names, or "extra_tools.NAME".
FAMILIES: dict[str, dict] = {
    "video editing and watching videos": {
        "tools": ("edit_video", "video_info", "watch_video"),
        "words": "video clip movie mp4 mov trim cut join merge speed slow captions subtitles gif reels vertical "
                 "compress mute audio extract rotate export youtube watch summarise transcript frame",
        "prompts": ("video_edit", "video")},
    "documents, PDFs, OCR and conversions": {
        "tools": ("convert_document", "ocr_copy", "data_to_sheet", "create_pdf", "export_doc_pdf"),
        "words": "convert conversion pdf word docx document markdown html epub txt ocr scan text image copy "
                 "table data excel translate file language hindi export google doc",
        "prompts": ("convert",)},
    "spreadsheets": {
        "tools": ("make_spreadsheet", "edit_spreadsheet"),
        "words": "spreadsheet sheet excel xlsx csv numbers table rows columns cell formula total sum invoices "
                 "receipts budget",
        "prompts": ("sheets",)},
    "translation": {
        "tools": ("translate_screen",),
        "words": "translate translation language foreign page window say english hindi spanish french german",
        "prompts": ("translate",)},
    "image generation": {
        "tools": ("make_image",),
        "words": "image picture photo draw drawing generate art sketch illustration logo wallpaper playground",
        "prompts": ("imagegen",)},
    "screen recording and finding screenshots": {
        "tools": ("screen_record", "find_screenshot"),
        "words": "record recording screen video capture area window screenshot screenshots find old",
        "prompts": ("screenrec", "screenshots")},
    "Apple apps: Notes, Reminders lists, calendar changes, Contacts, Maps, Safari, Photos, Pages/Keynote/Numbers": {
        "tools": ("notes", "reminders_manage", "calendar_manage", "contacts", "maps", "safari", "photos", "iwork"),
        "words": "note notes append grocery list reminders due overdue complete done reschedule calendar move "
                 "event rename contact phone number email birthday address maps directions route eta drive "
                 "safari reading list bookmarks photos album pictures pages keynote numbers presentation slides",
        "prompts": ("apple_apps",)},
    "tidying and merging folders": {
        "tools": ("tidy", "merge_folders"),
        "words": "tidy clean organise organize sort folder downloads desktop merge folders duplicates",
        "prompts": ("tidy", "merge")},
    "meetings and Google Meet calls": {
        "tools": ("meeting", "google_meet"),
        "words": "meeting call record notes transcript zoom teams huddle facetime google meet join share screen "
                 "action items standup",
        "prompts": ("meetings", "meet_call")},
    "automations, routines and Shortcuts": {
        "tools": ("automation", "run_routine", "shortcut", "make_shortcut"),
        "words": "automation schedule every day daily morning trigger when routine shortcut shortcuts siri "
                 "recurring later at",
        "prompts": ("automations", "apple_shortcuts", "shortcut_maker")},
    "teaching Mint and tutoring": {
        "tools": ("teach", "tutor"),
        "words": "teach watch me learn show you how tutor explain lesson practice quiz homework",
        "prompts": ("teach", "tutor")},
    "daily briefing": {
        "tools": ("briefing",),
        "words": "briefing brief morning summary day news agenda today",
        "prompts": ("briefing",)},
    "email triage": {
        "tools": ("mail", "read_email"),
        "words": "mail email inbox triage unread important reply draft archive read",
        "prompts": ("mailtriage",)},
    "trackers: tell me when something finishes": {
        "tools": ("track",),
        "words": "track tracker tell notify ping let me know when finishes done download build upload export",
        "prompts": ("trackers",)},
    "notifications": {
        "tools": ("notifications", "notify"),
        "words": "notifications notification center missed dismiss clear reply alert banner",
        "prompts": ("notifications",)},
    "dictation": {
        "tools": ("dictation",),
        "words": "dictation dictate type what i say voice typing",
        "prompts": ("dictation",)},
    "connectors to apps and services": {
        "tools": ("connector",),
        "words": "connector connect integration integrate service app notion things setup status",
        "prompts": ("connector_maker",)},
    "coding agents and sub-agents": {
        "tools": ("claude_mode", "delegate_tasks", "create_agent", "list_agents"),
        "words": "claude code codex agent agents coding session terminal delegate sub-agent job jobs stop "
                 "message tell tests test passed failing green broke usage limits left standup recap handoff "
                 "hand off continue",
        "prompts": ("notch_agents",)},
    "handing files to an app": {
        "tools": ("hand_to_app",),
        "words": "open with keka zip compress unzip extract unarchiver preview drop drag into app files",
        "prompts": ("handoff",)},
    "your orb, notch, voice, cards and showing things on screen": {
        "tools": ("show_on_screen", "mark_area", "clear_marks", "move_orb", "display_mode", "notch_files",
                  "screen_share_visibility", "set_voice", "show_card"),
        "words": "show point highlight underline circle box arrow mark where orb move up down left right "
                 "circle notch island dynamic orb mode voice deeper cheerful accent card count list tiles "
                 "screen share hide visible",
        "prompts": ("cards",)},         # SHOWING stays in the prompt: without it "a little down" went to set_preference
    "rewriting selected text": {
        "tools": ("edit_selection",),
        "words": "selected selection rewrite formal grammar shorten bullets polish rephrase",
        "prompts": ("rewrite",)},
    "skills, memory admin and the history of what happened": {
        "tools": ("create_skill", "update_skill", "list_skills", "delete_skill", "skill_history", "learn_skill",
                  "list_memories", "memory_used", "show_skills_and_memory"),
        "words": "skill skills save how to learned memory memories brain history yesterday last week earlier "
                 "undo learn this page clipboard",
        "prompts": ()},
    "hearing fixes": {
        "tools": ("fix_hearing",),
        "words": "mishear misheard hearing word wrong pronounce",
        "prompts": ()},
    "Mac and Mint odds and ends": {
        "tools": ("frontmost_app", "list_windows", "open_slack", "list_accounts", "chat_action", "show_chat",
                  "quit_mint", "update", "pointer", "wait_for_text", "preview_site"),
        "words": "front window windows slack workspace channel accounts chat clear summarize new session quit "
                 "update version mouse pointer wait text appear localhost preview site",
        "prompts": ()},
}

BUDGET = 6000              # chars one find_tools answer may use
GUIDE_MAX = 2600           # of which a family's how-to guidance
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
            description=("Find one of your less used tools (listed under 'More tools' in your instructions): "
                         "describe the job in a few words ('trim a video', 'translate a document', 'move my "
                         "orb'). Returns the matching tools with their exact arguments and how to use them; "
                         "then call use_tool. Call it before saying you can't do something."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "query": types.Schema(type=S, description="The job, in plain words, or a tool's name."),
            }, required=["query"])),
        types.FunctionDeclaration(
            name="use_tool",
            description=("Run a tool that find_tools showed you (or that your instructions name but your tool "
                         "list does not have). It runs exactly like a direct call: same checks, same result."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "name": types.Schema(type=S, description="The tool's exact name, e.g. edit_video."),
                "args": types.Schema(type=S, description="Its arguments as a JSON object, e.g. "
                                                         "{\"instruction\": \"cut the first 10 seconds\"}. "
                                                         "{} for none."),
            }, required=["name"])),
    ]


def live_tools(full: list[types.Tool]) -> list[types.Tool]:
    """What a voice session declares: the core tools and the bridge, or everything with the diet off.
    Also remembers the full list, for find_tools and use_tool."""
    remember(full)
    _served.clear()                    # a new session: the model has read nothing yet
    if not enabled():
        return full
    hidden = {n for n in _catalog if n not in CORE}
    kept = []
    for tool in full:
        decls = [_with_hints(d, hidden) for d in tool.function_declarations or [] if d.name in CORE]
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

def _sources() -> dict:
    """The modules whose prompt sections the families carry (imported here so the export can move them)."""
    from mint.tools import apple_apps
    from mint.tools import shortcuts as apple_shortcuts
    from mint.tools import automations
    from mint.tools import briefing
    from mint.tools import cards
    from mint.tools import connector_maker
    from mint.tools import convert
    from mint.voice import dictation
    from mint.tools import extra as extra_tools
    from mint.tools import handoff
    from mint.tools import imagegen
    from mint.knowledge import journal
    from mint.tools import mailtriage
    from mint.app import meet_call
    from mint.tools import meetings
    from mint.tools import merge
    from mint.ui import notch_agents
    from mint.tools import notifications
    from mint.tools import rewrite
    from mint.tools import screenrec
    from mint.tools import screenshots
    from mint.tools import sheets
    from mint.tools import shortcut_maker
    from mint.knowledge import teach
    from mint.tools import tidy
    from mint.tools import trackers
    from mint.tools import translate
    from mint.ui import tutor
    from mint.tools import video
    from mint.tools import video_edit
    return {"apple_apps": apple_apps, "apple_shortcuts": apple_shortcuts, "automations": automations,
            "briefing": briefing, "cards": cards, "connector_maker": connector_maker, "convert": convert,
            "dictation": dictation, "extra_tools": extra_tools, "handoff": handoff, "imagegen": imagegen,
            "journal": journal, "mailtriage": mailtriage, "meet_call": meet_call, "meetings": meetings,
            "merge": merge, "notch_agents": notch_agents, "notifications": notifications, "rewrite": rewrite,
            "screenrec": screenrec, "screenshots": screenshots, "sheets": sheets, "shortcut_maker": shortcut_maker,
            "teach": teach, "tidy": tidy, "trackers": trackers, "translate": translate, "tutor": tutor,
            "video": video, "video_edit": video_edit}


def _section(source: str) -> str:
    """'video_edit' -> video_edit.PROMPT; 'extra_tools.SHOWING' -> that constant."""
    module, _, attr = source.partition(".")
    try:
        found = _sources().get(module)
        return str(getattr(found, attr or "PROMPT", "") or "") if found is not None else ""
    except Exception:
        log.debug("no prompt section %s", source, exc_info=True)
        return ""


def guide(family: str) -> str:
    if family not in _guides:
        parts = [_section(s).strip() for s in FAMILIES.get(family, {}).get("prompts", ())]
        _guides[family] = "\n".join(p for p in parts if p)
    return _guides[family]


def prompt_line() -> str:
    families = "; ".join(FAMILIES)
    return ("More tools (not in your list: find_tools finds them, use_tool runs them): " + families + ". "
            "Any tool these instructions name that is not in your list is one of them - call use_tool with that "
            "name (find_tools first if you don't know its arguments). Before saying you can't do something, "
            "find_tools. One of them doing the whole job in one call (convert or translate a document, edit a "
            "video, make an image) is quick: do it yourself, not as a background_task. They follow the same rules: never send, share, record, post or start a call unless "
            "the user asked; never say you did something without a tool result that shows it.")


def prompt_parts(parts: list[str]) -> list[str]:
    """The prompt sections for a voice session: the families' own sections leave (find_tools serves them)
    and one line says what else there is."""
    if not enabled():
        return parts
    moved = {guide_text for family in FAMILIES for s in FAMILIES[family]["prompts"]
             if (guide_text := _section(s).strip())}
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
    best = rank(query)
    core_hits = [n for n in CORE if n in _all() and (n == query.lower() or set(_words(n)) & set(_words(query))
                                                    and n.replace("_", " ") in query.lower())]
    if not best:
        return (f"No hidden tool matches '{query}'. "
                + (f"You already have: {', '.join(core_hits)}. " if core_hits else "")
                + "The families: " + "; ".join(f"{f} ({', '.join(i['tools'])})" for f, i in FAMILIES.items())
                + ". Try other words, or do it with the tools you have.")[:budget]
    head = f"Tools for '{query}' - run one with use_tool(name, args=JSON object):"
    out, used = [head], len(head)
    first_family = family_of(best[0])
    shown, short = [], []
    for name in best:
        decl = _all()[name]
        entry = f"\n- {name}: {_named(decl.description or '')}\n  args: {_schema(decl)}"
        if used + len(entry) > budget - 200 and shown:
            short.append(name)
            continue
        out.append(entry[: budget - used - 200] if not shown else entry)
        used += len(out[-1])
        shown.append(name)
    if short:
        line = "\nAlso (find_tools with the name for its arguments): " + ", ".join(
            f"{n} ({(_all()[n].description or '').split('. ')[0][:70]})" for n in short)
        out.append(line)
        used += len(line)
    text = guide(first_family)
    if text and used < budget - 120:
        _served.add(first_family)
        room = min(GUIDE_MAX, budget - used - 40)
        out.append(f"\nHow to use them:\n{text[:room]}" + ("…" if len(text) > room else ""))
    if core_hits:
        out.append(f"\n(Already in your tools: {', '.join(core_hits)}.)")
    return "".join(out)[:budget]


# --- use_tool -------------------------------------------------------------------------------------------

def unwrap(name: str, args: dict | None) -> tuple[str, dict, str]:
    """(the real tool, its arguments, '') for a use_tool call - or (name, args, why not). Any other call
    comes back as it is. The caller runs the real tool through its normal path."""
    args = dict(args or {})
    if name != "use_tool":
        return name, args, ""
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

