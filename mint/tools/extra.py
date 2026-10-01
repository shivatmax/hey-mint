"""Skills, memories, and making every app reachable - layered over tools.py.

tools.py wraps its `tools()` and `dispatch()` with this module (see the end of
that file), so these tools and hooks live here instead of being threaded
through it. What the hooks add to every call:

* open_app resolves the name against installed apps first ("Xcode" -> ZCode).
* Before any screen action, an Electron/Chromium app in front is unlocked
  (axkit.unlock) so its full accessibility tree is there to act on.
* type_text finds the right box itself - the app's main prompt/message box,
  or the one named in `field` ("search box") - instead of refusing because
  nothing had focus, which is how "type into ChatGPT" failed in testing.
* After an app is opened or switched to, its saved skills are mentioned.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import AppKit
from google.genai import types

from mint.tools import appfinder
from mint.screen import axkit
from mint.app import live
from mint.knowledge import memory as membank
from mint.knowledge import notes
from mint.knowledge import skills as skillbook

log = logging.getLogger("mint.tools.extra")

STRING = {"type": types.Type.STRING}
BOOL = {"type": types.Type.BOOLEAN}
NUMBER = {"type": types.Type.NUMBER}
LIST = {"type": types.Type.ARRAY, "items": {"type": types.Type.STRING}}

PROMPT = """Skills (your learned how-tos): before a task in an app or website that \
takes more than two steps, call find_skill with the task (and the app) first. \
It answers in about a second with the steps of the best saved skill, or says \
none fits. Follow a skill's steps, then call skill_result. When the user teaches \
you ("no, click New project", "next time use the sidebar") or asks you to save \
how to do something, call create_skill or update_skill with the steps that \
worked. Long tasks are also learned automatically in the background. Never put \
passwords, keys or card numbers in a skill or a memory.

Every app is reachable. ChatGPT, ZCode, Slack, VS Code and Claude are \
Electron apps; Mint unlocks their full accessibility tree automatically, so \
their controls can be read and clicked. Never tell the user an app is \
blocked or ask them to click for you; try another way. type_text finds an \
app's main message or prompt box by \
itself; to type into a particular box, pass `field` ("search box", "project \
name"). If an app name is not found, open_app picks the closest installed \
app (speech often mishears names: "Xcode" for ZCode). To close a menu, \
popup or dialog, use ui_act with action "dismiss" - some menus (ChatGPT's \
project picker) ignore Escape.

Context: the first tool result of each request (plan_task's, if you plan) may \
end with [Context for this request] - a skill Jev chose and the memories \
relevant to the request. When a skill is given, follow ITS steps and redo your \
plan to match; do not keep a plan made before it arrived.

Apps before websites: when the user names something that is an installed app \
(ChatGPT, Slack, ZCode, Claude, VS Code, Notion…), open the app with open_app. \
Use the website only if they say "website", "in Chrome" or "in the browser".

Never quit or close an app, window or tab to recover from a problem - that \
destroys the user's work. Quit only when the user asks."""

SHOWING = """Showing, not just telling: when the user asks you to find something, \
where something is, or to point it out ("find the deadline in this PDF", "where \
does it mention refunds", "underline the part you mean"), call show_on_screen - \
it scrolls to it and boxes, underlines, highlights, circles or points at it, and \
your orb flies beside it. When explaining something that is on screen, mark the \
exact part as you talk about it (underline or highlight suits explanations). For \
images or charts with no words, look, then mark_area. Your orb can move: \
"go a little down", "you're covering that" -> move_orb (away), "circle around \
the window" -> move_orb (circle). Every time the user asks you to move, call \
move_orb - never say you moved without calling it."""

# Reliability run 20260929-215652: "1 week ago" (9 days) was taken as within the last 7 days; "saved in
# September" was done by the printed date because "3 weeks ago" / "1 month ago" can't say which month; a
# correction was redone by moving files back and forth; a file "edited" in an IDE was never saved.
FILE_CARE = """Files - dates, corrections and edits:
- Ages in find_files and read_file ("1 week ago", "1 month ago") are rounded down: "1 week ago" can mean \
13 days, "1 month ago" 59. When the request depends on when files were modified or saved ("in the last 7 \
days", "saved in September", "since Friday"), get each candidate's exact date with file_action action=info \
and compare it with today's date before choosing. Never quietly swap the rule (the date printed inside a \
file for its save date, or the other way round); if the user's rule can't be applied, say so.
- When the user corrects which files a move should cover, don't undo and redo blindly: first work out the \
right set from the facts - check EVERY candidate, including the files you did not move the first time \
(read each one) - compare it with where each file is now, and move only the ones in the wrong place - one move \
per file, never back and forth. Then list the folder once and \
check it holds exactly those files before saying it is done.
- Change a file's text with write_file: overwrite with the complete new text, or replace with `find` = a \
short exact piece (one sentence is enough, e.g. to put a title above the first sentence). Never edit a file \
by opening it in an app and editing the selection - that is not saved to disk. If write_file refuses, do what \
it says (append, replace, or ask the user one short question); never work around it with AppleScript, another \
app or a temporary file. After changing a file, read_file it to check the change is really there.
- Keep to the kind of file the user names: "Markdown notes" means .md files only, "the PDFs" means .pdf.
- Web research: when a page lists items and the details are on linked pages ("click one for its details"), \
open each linked page and read the details before comparing or building a table; never leave a \
column blank or guess it because it wasn't on the first page.
- Save made files where the user said (save_to on make_spreadsheet, data_to_sheet, create_pdf; the \
exact file name if they gave one). Don't leave scratch files in their folders: data you already have \
goes to data_to_sheet as `data`, not into a temporary .csv.
- Sums, totals and conversions of more than a few numbers: don't add them up in your head - use calculate \
(or let make_spreadsheet or edit_spreadsheet total them) and use that result."""

EXPRESSIVE = """You have a small, cute body: the orb, with a face and little hands. \
When the user asks for an expression - "smile", "dance for me", "show me a \
heart", "clap", "cry", "wave", "sunglasses" - call express with requested=true. \
Unasked, use it only at a genuinely emotional moment (warm praise or thanks: \
blush), at most once in a conversation; never on ordinary turns - the orb \
already reacts to your work by itself, and each call delays your answer. \
set_voice changes your voice and speaking style when asked; voices cannot be \
cloned or trained from a recording, so offer the closest prebuilt voice and a \
style instead."""


def _fn(name, description, properties, required=None):
    return types.FunctionDeclaration(
        name=name, description=description,
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={k: types.Schema(**v) for k, v in properties.items()},
                                required=required or []))


def declarations() -> list[types.FunctionDeclaration]:
    return [
        _fn("find_skill",
            "Find the saved skill (a learned how-to) for a task. Call BEFORE starting any "
            "multi-step task in an app or site. Jev picks the best one from the library in about "
            "a second; the result is its steps, or 'none fits'.",
            {"task": {**STRING, "description": "The task in the user's terms, e.g. 'create a new project in ChatGPT'."},
             "app": {**STRING, "description": "The app or site involved, if known."}},
            ["task"]),
        _fn("skill_result",
            "Report how following a skill went, so good skills rise and bad ones get fixed.",
            {"name": {**STRING}, "worked": {**BOOL},
             "note": {**STRING, "description": "What was different or learned, if anything."}},
            ["name", "worked"]),
        _fn("create_skill",
            "Save a new skill: how to do a kind of task, as steps that worked. Use when the user "
            "teaches you or asks you to remember how to do something, or after working out a "
            "tricky task.",
            {"title": {**STRING, "description": "Short imperative title, e.g. 'Create a project in ChatGPT'."},
             "when": {**STRING, "description": "When to use it, in terms of what the user asks."},
             "steps": {**LIST, "description": "Steps in order, naming the tool and exact on-screen labels."},
             "notes": {**LIST, "description": "Gotchas and the user's corrections."},
             "category": {**STRING, "description": "Folder, e.g. apps/chatgpt, coding/claude-code. Leave empty to let Mint file it."},
             "apps": {**STRING, "description": "App names involved, comma separated."}},
            ["title", "when", "steps"]),
        _fn("update_skill",
            "Change a saved skill: replace its steps, add a note (e.g. the user's correction), or "
            "change when it applies.",
            {"name": {**STRING, "description": "The skill's name or title."},
             "steps": {**LIST, "description": "New full list of steps, if they changed."},
             "note": {**STRING}, "when": {**STRING}},
            ["name"]),
        _fn("list_skills", "List saved skills by category, ranked by how well they work.",
            {"category": {**STRING}}),
        _fn("delete_skill", "Remove a saved skill (a copy is archived). Only when the user asks.",
            {"name": {**STRING}}, ["name"]),
        _fn("remember",
            "Save a fact about the user for future conversations: people, accounts by name, work, "
            "schedule, preferences, places, or a word you misheard. One fact per call. Jev files it "
            "and replaces an older version of the same fact. Never passwords, keys or card numbers.",
            {"fact": {**STRING, "description": "One standalone sentence, e.g. 'The user's manager is Priya.'"},
             "group": {**STRING, "description": "Optional: core, people, work, accounts, schedule, preferences, places, vocabulary, misc."},
             "fixed": {**BOOL, "description": "True for a fixed memory, given to every conversation (who the user is, standing instructions)."}},
            ["fact"]),
        _fn("recall",
            "Look up what you remember that is relevant to a question. Jev reads every saved fact "
            "against it and returns only the relevant ones, in about a second. Call it whenever an "
            "answer or action may depend on the user's people, accounts, work, schedule or "
            "preferences, and before saying you do not know something personal.",
            {"question": {**STRING, "description": "What you need to know, in plain words."}},
            ["question"]),
        _fn("update_memory",
            "Change a saved fact (the user says it changed or was wrong), or make it fixed / not fixed.",
            {"what": {**STRING, "description": "Which memory, in the user's words."},
             "new_fact": {**STRING, "description": "The corrected fact, if the text changes."},
             "fixed": {**BOOL}, "group": {**STRING}},
            ["what"]),
        _fn("forget", "Forget a saved fact the user no longer wants kept.",
            {"what": {**STRING}}, ["what"]),
        _fn("list_memories", "List what is remembered, optionally one group.",
            {"group": {**STRING}}),
        _fn("memory_used",
            "Which remembered facts you were given in this conversation (and the fixed ones) - for 'why did you "
            "say that?', 'which memory did you use?', 'how do you know that?'. Lets the user fix a wrong one.",
            {"minutes": {**NUMBER, "description": "how far back (default 30)"}}),
        _fn("show_skills_and_memory",
            "Open the Skills & Memory window on screen, where the user can see and edit every skill "
            "and every remembered fact. Use when they ask to see, check, review or edit your skills "
            "or your memory ('show me your skills', 'what do you remember about me - let me see').",
            {"tab": {**STRING, "enum": ["skills", "memory"]}}),
        _fn("show_on_screen",
            "Show the user where something is, in whatever app is in front (a PDF, a web page, a "
            "chat, a document, code): it finds the words on screen - or, for things without words "
            "(an icon, a button, a picture, a chart), looks at the screen for them - scrolling the "
            "window if needed, and draws a box, underline, highlight, circle or arrow around it, with "
            "an optional short note; your orb flies over beside it. Use for 'find X', 'where does it "
            "say', 'show me', 'point to', 'underline the part', and while explaining something on "
            "screen, to mark the exact part you are talking about.",
            {"text": {**STRING, "description": "The words to find, as they appear on screen (a few "
                                               "distinctive words work best), or a short description."},
             "style": {**STRING, "enum": ["box", "underline", "highlight", "circle", "arrow"]},
             "note": {**STRING, "description": "Optional note shown beside the mark, a few words."},
             "scroll": {**BOOL, "description": "Scroll the window to look for it if not visible (default true)."},
             "app": {**STRING, "description": "The app it is in, if the user said where ('this PDF' -> Preview, "
                                              "'the browser' -> Google Chrome) or it may not be in front. It is "
                                              "brought forward first."}},
            ["text"]),
        _fn("mark_area",
            "Mark a region with no text (an image, a chart, an icon) using 0-1000 coordinates of your "
            "latest look screenshot: x, y of its top-left corner, and its width and height.",
            {"x": {**NUMBER}, "y": {**NUMBER}, "w": {**NUMBER}, "h": {**NUMBER},
             "style": {**STRING, "enum": ["box", "circle", "arrow", "highlight"]}, "note": {**STRING}},
            ["x", "y", "w", "h"]),
        _fn("clear_marks", "Remove the marks you drew on screen.", {}),
        _fn("move_orb",
            "Move your orb on screen when the user asks: 'go a little down', 'move up', 'move left a "
            "lot', 'you are covering that' (direction away), 'go to the middle'. It stays there. For "
            "a corner, use set_preference position instead. 'back' returns to where it was before the "
            "last move. 'circle' goes once round the front window ('circle around the window', 'show "
            "me you can move') and comes home. Tricks, each a short fun move that ends back home: "
            "'hops' (hops along and back), 'loop' (a loop-the-loop), 'figure8', 'bounce' (like a "
            "ball), 'zigzag' (bumblebee flight), 'chase' (chases a sparkle), 'peek' (darts out and "
            "looks around), 'spin' (spinning hop); 'wander' or 'trick' = a random one ('do a trick', "
            "'show off', 'move around a bit'). For SEVERAL tricks ('do all your tricks', 'do a loop "
            "then a bounce') pass them in `tricks` (or ['all']) - ONE call plays them back to back; do "
            "not call once per trick. Only say you moved after this tool confirms it.",
            {"direction": {**STRING, "enum": ["up", "down", "left", "right", "away", "center", "back", "circle",
                                              "wander", "trick", "hops", "loop", "figure8", "bounce", "zigzag",
                                              "chase", "peek", "spin"]},
             "amount": {**STRING, "enum": ["little", "medium", "lot"]},
             "tricks": {**LIST, "description": "Several tricks in order, played back to back in one go: "
                        "hops, loop, figure8, bounce, zigzag, chase, peek, spin, or 'all'. Use with direction 'trick'."}},
            ["direction"]),
        _fn("screen_share_visibility",
            "Show or hide Mint in screen sharing, recordings and screenshots. Hidden is the default "
            "(people in a Google Meet or Zoom share do not see Mint). Use visible=true when the user "
            "says 'be visible', 'show yourself on the screen share', 'I want to show them you'; "
            "visible=false for 'hide from the share', 'go invisible'.",
            {"visible": {**BOOL}}, ["visible"]),
        _fn("display_mode",
            "Switch how Mint looks on screen: 'orb' = the floating round orb (default); 'notch' = Mint "
            "lives in the MacBook's camera notch like the iPhone's Dynamic Island (it grows out of the "
            "notch to show words, tasks and controls). Use for 'go into the notch', 'dynamic island mode', "
            "'be the notch', 'become the notch', 'go to my notch', 'go out of the notch', 'come out', 'back to "
            "the orb', 'floating mode'. The change plays right away as an animation (the orb flies into "
            "the notch, or drops out of it): say one short playful sentence.",
            {"mode": {**STRING, "enum": ["orb", "notch"]}}, ["mode"]),
        _fn("notch_files",
            "Spotlight in the notch (notch mode): show files, folders or apps as tiles the user can open, drag "
            "out, copy or Quick Look. action=show with `paths` (after you found files: 'show them in the notch'), "
            "action=search with `query` (files and apps by name: 'find my invoice', 'find Photoshop'), "
            "action=expand to grow it into a grid ('show more', 'bigger', 'as a 3 by 3 grid' with layout), "
            "action=collapse to shrink it back. Opening an app still uses open_app. In notch mode, whatever "
            "find_files or find_screenshot finds ALREADY appears in the notch by itself: then just say how many "
            "and what in one short sentence - never read a long list aloud.",
            {"action": {**STRING, "enum": ["show", "search", "expand", "collapse"]},
             "paths": {**LIST, "description": "Absolute file, folder or .app paths to show (action=show)."},
             "query": {**STRING, "description": "What to search for, or what the shown files are."},
             "title": {**STRING, "description": "A short heading, e.g. '3 PDFs about the lease'."},
             "layout": {**STRING, "enum": ["row", "3x1", "3x2", "3x3"]}},
            ["action"]),
        _fn("express",
            "Play an expression on your orb body. ONLY when the user asks for one ('smile', "
            "'clap', 'dance', 'show me a heart', 'cry', 'wave'), or at a genuinely emotional moment "
            "(they praise or thank you warmly) - at most once in a conversation unasked. 'show' (also "
            "'party', 'perform', 'put on a show') makes the whole critter family pop out of the toy box "
            "and perform together (~20 s). Never for "
            "ordinary answers, acknowledgements or yes/no: those need no expression.",
            {"emotion": {**STRING, "enum": [e for e in _EMOTES if e not in ("yes", "no")]},
             "requested": {**BOOL, "description": "True only if the user explicitly asked for this expression."}},
            ["emotion"]),
        _fn("set_voice",
            "Change your speaking voice and/or style when the user asks ('use a deeper voice', "
            "'sound more cheerful', 'talk slower', 'use Puck'). Gemini voices cannot be cloned or "
            "trained on a recording; you choose one of the prebuilt voices and a speaking style. "
            "The change applies within a few seconds and the conversation continues.",
            {"voice": {**STRING, "description": "A prebuilt voice name, or empty to keep the current one. " + _VOICE_HELP},
             "style": {**STRING, "description": "How to speak, e.g. 'warm and calm', 'cheerful and fast', "
                                                "'soft British accent'. 'default' clears it."}}),
    ]


def _find_skill(args: dict) -> str:
    skill, why = skillbook.find(str(args.get("task", "")), str(args.get("app", "")))
    if skill is None:
        return f"No skill: {why} Work it out, and if it takes many steps it will be learned."
    skillbook.mark_used(skill)
    from mint.knowledge.learner import learner
    learner.note_skill(skill["name"])
    print(f"  [skill chosen: {skill['category']}/{skill['name']} - {why}]", flush=True)
    return skillbook.instructions_for(skill)


def _create_skill(args: dict) -> str:
    skill, message = skillbook.create(
        str(args.get("title", "")), str(args.get("when", "")), args.get("steps") or [],
        args.get("notes") or [], category=str(args.get("category", "")),
        apps=str(args.get("apps", "")), source="user" if args.get("from_user") else "mint")
    if skill is not None:
        print(f"  [skill saved: {skill['category']}/{skill['name']}]", flush=True)
    return message


def _update_skill(args: dict) -> str:
    _, message = skillbook.update(str(args.get("name", "")), steps=args.get("steps") or None,
                                  add_note=args.get("note") or None, when=str(args.get("when", "")))
    return message


from mint.ui.emotes import EMOTES as _EMOTES  # noqa: E402

VOICES = {
    "Zephyr": "bright", "Puck": "upbeat", "Charon": "informative", "Kore": "firm", "Fenrir": "excitable",
    "Leda": "youthful", "Orus": "firm", "Aoede": "breezy", "Callirrhoe": "easy-going", "Autonoe": "bright",
    "Enceladus": "breathy", "Iapetus": "clear", "Umbriel": "easy-going", "Algieba": "smooth",
    "Despina": "smooth", "Erinome": "clear", "Algenib": "gravelly", "Rasalgethi": "informative",
    "Laomedeia": "upbeat", "Achernar": "soft", "Alnilam": "firm", "Schedar": "even", "Gacrux": "mature",
    "Pulcherrima": "forward", "Achird": "friendly", "Zubenelgenubi": "casual", "Vindemiatrix": "gentle",
    "Sadachbia": "lively", "Sadaltager": "knowledgeable", "Sulafat": "warm",
}
from mint.voice.voices import GENDER as _GENDER  # noqa: E402

_VOICE_HELP = ("Voices (only these 30 work live): "
               + ", ".join(f"{k} ({_GENDER.get(k, '')}, {v})" for k, v in VOICES.items())
               + ". Asked for the options, name a few that fit and mention the Settings voice picker.")


def _live_mint():
    """The running session object, found without importing it into the UI."""
    import gc

    from mint.app.session import Mint
    return next((o for o in gc.get_objects() if isinstance(o, Mint) and o.loop is not None), None)


def _set_voice(args: dict) -> str:
    from mint.core import config
    from mint.core import prefs
    wanted = str(args.get("voice", "") or "").strip()
    style = str(args.get("style", "") or "").strip()
    changed = []
    if wanted:
        match = next((v for v in VOICES if v.lower() == wanted.lower()), None)
        if match is None:
            return f"Unknown voice '{wanted}'. {_VOICE_HELP}"
        if match != config.VOICE:
            prefs.set("voice_name", match)
            config.VOICE = match
            changed.append(f"voice {match} ({VOICES[match]})")
    if style and (style if style.lower() not in {"default", "normal", "none", "reset"} else "") == (prefs.get("speaking_style") or ""):
        style = ""                       # already speaking that way
    if style:
        prefs.set("speaking_style", "" if style.lower() in {"default", "normal", "none", "reset"} else style)
        changed.append("style: " + (style if style.lower() not in {"default", "normal", "none", "reset"}
                                     else "back to default"))
    if not changed:
        # In live testing, "what voice are you using?" right after a switch made
        # the model call set_voice again, and every call reconnected. Nothing
        # changed, so nothing to do.
        return (f"No change needed: you are already using {config.VOICE} ({VOICES.get(config.VOICE, '')})"
                + (f", style '{prefs.get('speaking_style')}'" if prefs.get("speaking_style") else "") + ".")
    mint = _live_mint()
    if mint is not None:
        schedule_voice_reconnect(mint)
        when = "in a few seconds, once you finish this sentence"
    else:
        when = "from the next session"
    print(f"  [voice: {', '.join(changed)}]", flush=True)
    return (f"Changed {', '.join(changed)}. It takes effect {when}; the conversation carries on. "
            "Say one short sentence now, in your current voice.")


_reconnect_pending = [False]


def schedule_voice_reconnect(mint) -> None:
    """Apply a new voice or style (from set_voice or the Settings window)
    with one reconnect, however many changes arrive meanwhile."""
    if _reconnect_pending[0] or mint is None or mint.loop is None:
        return
    _reconnect_pending[0] = True
    asyncio.run_coroutine_threadsafe(_reconnect_when_quiet(mint), mint.loop)


async def _reconnect_when_quiet(mint) -> None:
    """Reconnect with the resumption handle so the new voice applies and the
    conversation is kept (verified: a resumed session recalled earlier context
    in the new voice). Waits until Mint has finished speaking."""
    try:
        await _reconnect_quietly(mint)
    finally:
        _reconnect_pending[0] = False


async def _reconnect_quietly(mint) -> None:
    await asyncio.sleep(1.5)
    for _ in range(60):
        playing = getattr(mint.audio, "playing", False)
        if not playing and not getattr(mint, "_busy", False) and mint.audio_in.empty():
            break
        await asyncio.sleep(0.5)
    session = mint.session
    if session is not None:
        print("  [voice: reconnecting to apply it]", flush=True)
        try:
            await session.close()
        except Exception:
            log.debug("closing the session for a voice change failed", exc_info=True)


_last_unasked = [0.0]
UNASKED_EVERY = 90.0     # seconds between expressions the user did not ask for


def _express(args: dict) -> str:
    """In live testing the model called express on every turn ("yes, yes, smile,
    yes…"), each one a round trip before the answer. Unasked expressions are now
    rate-limited; ones the user asked for always play."""
    from mint.ui import emotes
    name = str(args.get("emotion", ""))
    try:
        from mint.app import live
        asked = live.request() or ""
    except Exception:
        asked = ""
    # "show me your emotions one by one": the user asked, even when the model forgets requested=true
    # (in testing 3 of 5 were skipped mid-list).
    if re.search(r"\b(emotion|emotions|expression|expressions|face|faces|express)\b", asked, re.I) or \
            (name and re.search(rf"\b{re.escape(name)}", asked, re.I)):
        args = dict(args, requested=True)
    if not args.get("requested"):
        now = time.monotonic()
        if name in ("yes", "no") or now - _last_unasked[0] < UNASKED_EVERY:
            return "Skipped: no expression needed here. Just answer; only use express when asked."
        _last_unasked[0] = now
    return emotes.play(name)


def _move_orb(args: dict) -> str:
    from mint.ui.motion import motion
    direction = str(args.get("direction", "")).lower()
    if args.get("tricks"):
        return motion.tricks(list(args.get("tricks") or []))
    if direction in ("wander", "trick"):
        return motion.wander(force=True)
    if direction in ("hops", "loop", "figure8", "bounce", "zigzag", "chase", "peek", "spin"):
        return motion.trick(direction)
    if direction == "circle":
        return motion.circle()
    return motion.nudge(direction, str(args.get("amount", "little")))


def _show_on_screen(args: dict) -> str:
    from mint.screen import pointer
    return pointer.show(str(args.get("text", "")), str(args.get("style", "box") or "box"),
                        str(args.get("note", "") or ""), args.get("scroll", True) is not False,
                        app=str(args.get("app", "") or ""))


def _mark_area(args: dict) -> str:
    from mint.screen import pointer
    return pointer.mark_area(float(args.get("x", 0)), float(args.get("y", 0)), float(args.get("w", 50)),
                             float(args.get("h", 50)), str(args.get("style", "box") or "box"),
                             str(args.get("note", "") or ""))


def _clear_marks(args: dict) -> str:
    from mint.ui.marks import marks
    marks.clear()
    return "Cleared the marks."


def _screen_share_visibility(args: dict) -> str:
    from mint.ui import sharing
    return sharing.set_visible(bool(args.get("visible")))


def _notch_files(args: dict) -> str:
    from mint.ui import notch
    try:
        from mint.ui import notch_search
    except ImportError:
        return "Search in the notch isn't installed yet."
    if not notch.active():
        return "That shows in notch mode only (display_mode notch). Tell the user the results instead."
    action = str(args.get("action", "show"))
    query, title = str(args.get("query", "") or ""), str(args.get("title", "") or "")
    layout = str(args.get("layout", "") or "")
    if layout:
        try:
            from mint.core import prefs
            prefs.set("notch_search_grid", layout)
        except Exception:
            pass
    if action == "search":
        notch_search.search(query, scope="all")
        notch.notch.tab, notch.notch.search_until = "search", __import__("time").monotonic() + 15
        return f"Searching for '{query}' in the notch; the user can scroll, open or drag the results."
    if action == "expand":
        notch_search.set_expanded(True)
        notch.notch.tab, notch.notch.search_until = "search", __import__("time").monotonic() + 20
        return "The notch opened bigger, as a grid" + (f" ({layout})" if layout else "") + "."
    if action == "collapse":
        notch_search.set_expanded(False)
        return "Back to the one-row view."
    paths = [str(p) for p in (args.get("paths") or []) if str(p).strip()]
    if not paths:
        return "No paths given to show."
    notch_search.show(paths=paths[:120], query=query, title=title)
    return f"Showing {len(paths)} item(s) in the notch: the user can open, drag out, copy or Quick Look them."


def _display_mode(args: dict) -> str:
    from mint.ui import notch
    return notch.set_mode(str(args.get("mode", "")))


HANDLERS = {
    "screen_share_visibility": _screen_share_visibility,
    "display_mode": _display_mode,
    "notch_files": _notch_files,
    "move_orb": _move_orb,
    "show_on_screen": _show_on_screen,
    "mark_area": _mark_area,
    "clear_marks": _clear_marks,
    "express": _express,
    "set_voice": _set_voice,
    "find_skill": _find_skill,
    "skill_result": lambda a: skillbook.record_result(str(a.get("name", "")), bool(a.get("worked")),
                                                      str(a.get("note", ""))),
    "create_skill": _create_skill,
    "update_skill": _update_skill,
    "list_skills": lambda a: skillbook.listing(str(a.get("category", ""))),
    "delete_skill": lambda a: skillbook.archive(str(a.get("name", ""))),
    "remember": lambda a: membank.add(str(a.get("fact", "")), str(a.get("group") or a.get("topic") or ""),
                                      pinned=a.get("fixed") if "fixed" in a else None),
    "recall": lambda a: membank.recall(str(a.get("question") or a.get("query") or "")),
    "update_memory": lambda a: membank.update(str(a.get("what", "")), str(a.get("new_fact", "")),
                                              fixed=a.get("fixed") if "fixed" in a else None,
                                              group=str(a.get("group", ""))),
    "forget": lambda a: membank.forget(str(a.get("what", ""))),
    "list_memories": lambda a: membank.listing(str(a.get("group", ""))),
    "memory_used": lambda a: membank.used(float(a.get("minutes") or 30)),
    "show_skills_and_memory": lambda a: _show_brain(str(a.get("tab") or "skills")),
}


def _show_brain(tab: str) -> str:
    from mint.ui import brain as brain_window
    brain_window.open_window("memory" if tab == "memory" else "skills")
    skills, blocks = skillbook.all_skills(), membank.blocks()
    return (f"Opened the Skills & Memory window on the {tab} tab: {len(skills)} skills, {len(blocks)} "
            "remembered facts. The user can edit them there; tell them briefly.")


def prompt_text() -> str:
    """Appended to the session instructions (through custom.prompt_addendum).

    Runs at every (re)connect, before the voice config is built - which is
    where a saved voice choice takes effect."""
    from mint.core import config
    from mint.ui import emotes
    from mint.core import prefs
    saved = prefs.get("voice_name")
    if saved in VOICES:
        config.VOICE = saved
    emotes.ensure_attached()
    from mint.tools import shortcuts as apple_shortcuts
    from mint.tools import automations
    from mint.tools import briefing
    from mint.tools import cards
    from mint.tools import convert
    from mint.voice import dictation
    from mint.tools import harness as harness_tools
    from mint.knowledge import journal
    from mint.tools import mac as macctl
    from mint.tools import apple_apps
    from mint.tools import calc
    from mint.tools import connector_maker
    from mint.tools import handoff
    from mint.tools import imagegen
    from mint.tools import merge
    from mint.tools import music
    from mint.tools import notifications
    from mint.tools import shortcut_maker
    from mint.tools import undo
    from mint.tools import agentapps
    from mint.ui import notch_agents
    from mint.tools import mailtriage
    from mint.tools import meetings
    from mint.tools import rewrite
    from mint.tools import screenrec
    from mint.tools import screenshots
    from mint.tools import sheets
    from mint.app import tasks
    from mint.knowledge import teach
    from mint.tools import tidy
    from mint.tools import trackers
    from mint.tools import translate
    from mint.ui import tutor
    from mint.tools import video
    from mint.tools import video_edit
    parts = [PROMPT, harness_tools.PROMPT, tasks.PROMPT, video.PROMPT, automations.PROMPT, journal.PROMPT,
             rewrite.PROMPT, sheets.PROMPT, tidy.PROMPT, teach.PROMPT, tutor.PROMPT,
             meetings.PROMPT, briefing.PROMPT, apple_shortcuts.PROMPT, screenshots.PROMPT,
             translate.PROMPT, mailtriage.PROMPT, macctl.PROMPT, screenrec.PROMPT, trackers.PROMPT, notifications.PROMPT, undo.PROMPT, calc.PROMPT, merge.PROMPT, imagegen.PROMPT, shortcut_maker.PROMPT, apple_apps.PROMPT, connector_maker.PROMPT, handoff.PROMPT, music.PROMPT, agentapps.PROMPT, notch_agents.PROMPT, cards.PROMPT, dictation.PROMPT, video_edit.PROMPT, convert.PROMPT, FILE_CARE, EXPRESSIVE, SHOWING]
    unfinished = tasks.prompt_text()
    if unfinished:
        parts.append(unfinished)
    parts.append(f"Your current voice is {config.VOICE}" + (f" ({VOICES[config.VOICE]})" if config.VOICE in VOICES else "")
                 + ". If asked which voice you use, just say so; do not call set_voice.")
    style = prefs.get("speaking_style")
    if style:
        parts.append(f"Speaking style the user asked for: {style}. Keep to it.")
    language = prefs.get("reply_language") or "auto"
    if language != "auto":
        parts.append(f"Always speak and write to the user in {language}, whatever language they use, unless "
                     "they ask for another language for one answer. Names, code and quoted text stay as they are. "
                     "If they want this changed for good ('answer in the language I speak', 'switch back to "
                     "English'), call set_preference with reply_language (auto, or the language) - saying so is "
                     "not enough.")
    index = skillbook.index_text()
    if index:
        parts.append("Saved skills (find_skill loads one):\n" + index)
    fixed = membank.pinned_text()
    if fixed:
        parts.append("Fixed memories (always true for this user):\n" + fixed)
    index = membank.index_text()
    if index:
        parts.append(f"More remembered facts, by group: {index}. They are NOT listed here to save "
                     "space - call recall with your question to get the relevant ones.")
    recent = recent_conversation()
    if recent:
        parts.append("The conversation so far (most recent last) - continue from it; a new "
                     "request may refer back to it. Something that failed or could not be found "
                     "there is not a fact about now: when the user asks again, try again with your "
                     "tools (a different way) before saying it can't be done:\n" + recent)
    return "\n\n".join(parts)


def recent_conversation(max_turns: int = 14, max_chars: int = 1800, within_hours: float = 6.0) -> str:
    """The last few turns from history.jsonl, if recent: after a restart or a new
    session, Mint still knows what was being talked about and done."""
    import json as _json

    from mint.knowledge.conversation import HISTORY
    try:
        lines = HISTORY.read_text().splitlines()[-200:]
    except OSError:
        return ""
    turns = []
    for line in lines:
        try:
            entry = _json.loads(line)
        except ValueError:
            continue
        if entry.get("role") in ("user", "mint", "jarvis") or (entry.get("role") == "tool"
                                                        and not entry.get("text", "").startswith(("find_skill", "recall"))):
            turns.append(entry)
    if not turns:
        return ""
    try:
        last = time.mktime(time.strptime(turns[-1]["t"], "%Y-%m-%d %H:%M"))
        if time.time() - last > within_hours * 3600:
            return ""
    except (KeyError, ValueError):
        return ""
    out = []
    for entry in turns[-max_turns:]:
        who = {"user": "User", "mint": "You", "jarvis": "You", "tool": "Tool"}[entry["role"]]
        out.append(f"[{entry['t'][11:]}] {who}: {entry['text'][:220]}")
    text = "\n".join(out)
    return text[-max_chars:]


# --- hooks around every tool call -------------------------------------------------------

_SCREEN = {"show_on_screen", "desktop", "click_text", "read_window", "look", "type_text", "click_at", "press_key", "ui_act",
           "scroll_to", "menu", "wait_for_text", "pointer"}
_BROWSERS = {"com.google.Chrome", "com.brave.Browser", "com.apple.Safari", "company.thebrowser.Browser",
             "com.microsoft.edgemac", "org.mozilla.firefox"}
_COMPOSER = re.compile(r"message|ask|prompt|chat|reply|type|write|describe|task|what|anything|send", re.I)


def _running(name: str):
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if (app.localizedName() or "").lower() == name.lower():
            return app
    return None


def prepare_typing(field: str = "") -> str:
    """Put the keyboard focus in the right box of the front app before typing.

    Returns '' when nothing needed doing, a short note on what was focused, or
    a FAILED message when a named field is not on screen.
    """
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return ""
    if front.bundleIdentifier() in _BROWSERS and not field:
        return ""                 # web pages: type_text's own editor focusing handles them
    axkit.unlock(front)
    window = axkit.focused_window(front.processIdentifier())
    if window is None:
        _debug(f"prepare_typing: {front.localizedName()} has no focused window")
        return ""
    # An open popup or dialog (ChatGPT's project picker) is its own window, in
    # front of the main one. Its boxes come first: in testing, typing a
    # project name meant for the picker's search box went into the main
    # message box instead, and Return sent it as a chat.
    for other in axkit.windows_front_to_back(front.processIdentifier()):
        if other == window:
            break
        popup_inputs = axkit.text_inputs(other)
        if popup_inputs:
            window = other
            break
    inputs = axkit.text_inputs(window)
    if not inputs:
        # Just after a click (ZCode's "New task") the view is still being
        # rebuilt and has no text box for a moment; give it a little time.
        import time as _time
        deadline = _time.monotonic() + 2.5
        while not inputs and _time.monotonic() < deadline:
            _time.sleep(0.35)
            window = axkit.focused_window(front.processIdentifier()) or window
            inputs = axkit.text_inputs(window)
    _debug(f"prepare_typing: {front.localizedName()} window '{axkit.attr(window, 'AXTitle')}' inputs: "
           + "; ".join(axkit.describe_input(i) + (" [focused]" if i["focused"] else "") for i in inputs[:8]))
    if not field:
        if not inputs:
            return ""
        # A box can claim AXFocused inside Chromium while the keyboard is not
        # really there (ChatGPT's composer, just after the app came forward), so
        # trust the app's own focused element instead.
        really = axkit.attr(axkit.AX.AXUIElementCreateApplication(front.processIdentifier()), "AXFocusedUIElement")
        if really is not None and axkit.attr(really, "AXRole") in axkit._INPUT_ROLES:
            return ""
        claimed = [i for i in inputs if i["focused"]]
        candidates = claimed or [i for i in inputs if i["role"] != "AXSearchField"] or inputs

        def fitness(item):
            x, y, w, h = item["frame"]
            return ((2 if _COMPOSER.search(item["label"]) else 0) + (1 if item["role"] == "AXTextArea" else 0)
                    + min(w * max(h, 20), 400_000) / 400_000 + y / 4000)   # big, and low on screen
        target = max(candidates, key=fitness)
    else:
        if not inputs:
            return (f"FAILED: there is no text box in the front window ({front.localizedName()}) to "
                    f"find '{field}' in. Open it first (e.g. click its button), then type.")
        wf = axkit.frame(window)
        options = {str(n): axkit.describe_input(i, wf) for n, i in enumerate(inputs[:250])}
        wanted = field.lower()
        exact = [n for n, i in enumerate(inputs) if wanted and wanted in i["label"].lower()]
        if len(exact) == 1:
            target = inputs[exact[0]]
        else:
            from mint.core import jev
            pick = jev.choose(field, options, instructions=(
                "Which of these text boxes in the app window is the one the user means by `request`? "
                "Choose none if none of them is it."))
            if pick is None or not pick.sure:
                return (f"FAILED: no text box matching '{field}' in {front.localizedName()}. Boxes there: "
                        + "; ".join(options.values())[:500] + ". It may need opening first (click its button).")
            target = inputs[int(pick.id)]
    if axkit.focus(target["element"]) and axkit.attr(target["element"], "AXFocused"):
        return f"(Focused the {axkit.describe_input(target)} first.)"
    # Some fields refuse programmatic focus; a real click always works.
    import time

    import Quartz
    x, y, w, h = target["frame"]
    point = Quartz.CGPointMake(x + min(w / 2, 60), y + h / 2)
    for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft))
        time.sleep(0.04)
    time.sleep(0.15)
    return f"(Clicked into the {axkit.describe_input(target)} first.)"


_target: dict = {"app": None, "at": 0.0}
TARGET_FOR = 90.0     # seconds an opened app stays "the one being worked in"


def _wait_running(name: str, timeout: float = 6.0):
    """The running app called `name`, waiting for a cold launch."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app = _running(name)
        if app is not None:
            return app
        time.sleep(0.2)
    return None


def _ensure_target_front() -> str:
    """'' if fine; a FAILED message if the app Mint just opened is not in
    front and cannot be brought there - typing then would land elsewhere."""
    app = _target["app"]
    if app is None or time.monotonic() - _target["at"] > TARGET_FOR or app.isTerminated():
        return ""
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.processIdentifier() == app.processIdentifier():
        return ""
    from mint.screen.ground import bring_forward
    if bring_forward(app, wait=2.0):
        return ""
    return (f"FAILED: {app.localizedName()} is not in front ({front.localizedName() if front else 'nothing'} is), "
            "and could not be brought forward, so nothing was done - acting now would hit the wrong app. "
            "Try switch_to it.")


def _debug(text: str) -> None:
    import os
    if os.environ.get("MINT_DEBUG"):
        print(f"  [debug] {text}", flush=True)


def _skill_hint(app_name: str) -> str:
    found = skillbook.for_app(app_name)[:4]
    if not found:
        return ""
    titles = "; ".join(f"'{s['title']}'" for s in found)
    return f"\n[Saved skills for {app_name}: {titles}. Call find_skill with the task to load one.]"


_NO_CONTEXT = {"music", "step_done", "task", "automation", "stop_listening", "express", "set_voice", "get_status",
               "set_preference", "show_chat", "notify", "set_timer", "media_key", "set_volume"}
_skill_asked = {"at": 0.0}


def _context_pack(request: str, want_skill: bool) -> str:
    """Jev's picks for this request: the skill to follow and the memories that
    matter. Both questions run at once, in parallel with the tool itself."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        skill_job = pool.submit(skillbook.find, request) if want_skill and skillbook.all_skills() else None
        memory_job = pool.submit(membank.relevant, request)
        skill, why = skill_job.result() if skill_job else (None, "")
        memories = memory_job.result()
    parts = []
    if skill is not None:
        skillbook.mark_used(skill)
        from mint.knowledge.learner import learner
        learner.note_skill(skill["name"])
        print(f"  [context: skill {skill['category']}/{skill['name']} - {why}]", flush=True)
        parts.append(skillbook.instructions_for(skill))
    if memories:
        print(f"  [context: {len(memories)} memories]", flush=True)
        parts.append("Relevant memories:\n" + "\n".join(f"- ({b['group']}) {b['text']}" for b in memories))
    return ("\n[Context for this request]\n" + "\n".join(parts)) if parts else ""


def plan_context(goal: str = "") -> str:
    """For the session's plan_task (which does not pass through wrap): the
    context pack for the current request, so the plan is made from the skill.
    Synchronous, about a second; '' if already delivered or nothing fits."""
    request = live.claim_context() or ""
    if not request and goal:
        request = goal
    if not request:
        return ""
    try:
        return _context_pack(request, time.monotonic() - _skill_asked["at"] > 90)
    except Exception:
        log.exception("plan context failed")
        return ""


async def wrap(core, name: str, args: dict):
    """Run a tool with the hooks above. `core` is tools.py's own dispatch."""
    args = dict(args or {})
    if name in HANDLERS:
        if name == "find_skill":
            _skill_asked["at"] = time.monotonic()
        return await asyncio.to_thread(HANDLERS[name], args), None

    if name == "quit_app" and not re.search(r"\b(quit|close|exit|kill|shut)\b", live.request(), re.I):
        # In testing Gemini "fixed" a stuck page by quitting Chrome - every
        # window the user had open. Quitting is only for when they ask.
        return ("NOT RUN: quitting an app closes all of the user's windows in it, and the user did not "
                "ask to quit anything. Recover another way (switch_to, a different ui_act target, Escape)."), None

    # Context pack: on the first real tool of a request, fetched concurrently.
    pack_task = None
    if name not in _NO_CONTEXT:
        request = live.claim_context()
        if request:
            want_skill = time.monotonic() - _skill_asked["at"] > 90
            pack_task = asyncio.create_task(asyncio.to_thread(_context_pack, request, want_skill))
    try:
        result, image = await _wrapped(core, name, args)
    finally:
        pack = ""
        if pack_task is not None:
            try:
                pack = await asyncio.wait_for(asyncio.shield(pack_task), 3.0)
            except Exception:
                pack = ""
    return result + pack, image


async def _wrapped(core, name: str, args: dict):

    note = ""
    if name == "open_app" and args.get("name"):
        resolved, how = await asyncio.to_thread(appfinder.resolve, str(args["name"]))
        if resolved is None:
            return f"Could not open '{args['name']}'. {how}", None
        if appfinder._squash(resolved) != appfinder._squash(str(args["name"])):
            note = f" ('{args['name']}' is not installed; opened the closest match, {resolved} - {how}.)"
            print(f"  [open_app: '{args['name']}' -> {resolved} ({how})]", flush=True)
        args["name"] = resolved

    if name in _SCREEN:
        await asyncio.to_thread(axkit.unlock)

    field = str(args.pop("field", "") or "") if name == "type_text" else ""
    if name in _SCREEN and name != "look":
        # Act on the app that was just opened, not on whatever else is in front.
        guard = await asyncio.to_thread(_ensure_target_front)
        if guard:
            return guard, None
    if name == "type_text":
        prepared = await asyncio.to_thread(prepare_typing, field)
        if prepared.startswith("FAILED"):
            return prepared, None
        note = (" " + prepared) if prepared else ""
        if prepared and args.get("press_return") and not field:
            # The box was chosen here, not by the model. Return in a guessed box
            # is how a stray message gets sent, so leave that to a second call.
            args["press_return"] = False
            note += (" Return was NOT pressed, because the box was chosen automatically - check it "
                     "is the right one, then press_key return (or name the box with field=).")

    result, image = await core(name, args)

    if name == "type_text" and result.startswith("FAILED") and "no text field" in result:
        # A menu, popover or dialog is in front (ChatGPT's project picker, in
        # testing) and its box is not in the window type_text looks at. ui_act
        # reads every control, dialogs first, and types into the one described.
        retry, image = await core("ui_act", {"action": "type", "text": args.get("text", ""),
                                             "target": field or "the text box for typing here",
                                             "press_return": bool(args.get("press_return"))})
        if not retry.startswith(("FAILED", "Unknown", "The ui_act")):
            return retry + " (typed through ui_act - the box was in a menu or dialog)", image
        result += f" ui_act also could not: {retry[:200]}"

    if name == "switch_to" and "no open tab matches" in result:
        # switch_to knows Chrome tabs; the user may mean an app.
        resolved, _ = await asyncio.to_thread(appfinder.resolve, str(args.get("what", "")))
        app = _running(resolved) if resolved else None
        if app is not None:
            from mint.screen.ground import bring_forward
            if await asyncio.to_thread(bring_forward, app, 2.0):
                _target["app"], _target["at"] = app, time.monotonic()
                result = f"Switched to the {resolved} app."

    if name in {"open_app", "switch_to"} and not re.search(r"^(Could not|FAILED)", result):
        target = args.get("name") or args.get("what") or ""
        app = await asyncio.to_thread(_wait_running, str(target)) if target else None
        if app is not None:
            _target["app"], _target["at"] = app, time.monotonic()
            from mint.screen.ground import bring_forward
            if not await asyncio.to_thread(bring_forward, app, 2.0):
                result += (f" WARNING: {app.localizedName()} opened but macOS kept another app in front; "
                           "it is not ready to type into yet.")
            await asyncio.to_thread(axkit.unlock, app, 0.8)
        result += _skill_hint(str(target))
    elif name in {"open_slack", "open_chrome"}:
        result += _skill_hint("Slack" if name == "open_slack" else "Chrome")
    return result + note, image


def patch_declarations(decls: list) -> list:
    """Give type_text its `field` parameter."""
    for decl in decls:
        if decl.name == "type_text" and decl.parameters and "field" not in (decl.parameters.properties or {}):
            decl.parameters.properties["field"] = types.Schema(
                type=types.Type.STRING,
                description=("Which box to type into, if not the one with focus: 'search box', 'prompt', "
                             "'project name'. Leave empty to use the focused box or the app's main "
                             "message box."))
    return decls
