"""Editing selected text by voice, in any app: "make this more formal", "fix the grammar",
"turn this into bullet points", "translate this to Hindi", "shorten it to one line".

edit_selection copies what is selected (the user's clipboard is put back after), has
Gemini Flash Lite rewrite it - told which app and window it is in, so an email reads
like an email and a Slack message like a Slack message - and pastes the result over
the selection. The app's own undo (⌘Z) brings the original back, and Mint keeps it
too ("undo that" -> action=undo pastes it back if the selection is still there).

Nothing that looks like a password, key or card number is sent anywhere.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("mint.tools.rewrite")

_last: dict = {}          # the last edit: original and new text, app, when


def _front() -> tuple[str, str]:
    try:
        import AppKit
        from mint.screen import axkit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        window = axkit.focused_window(app.processIdentifier()) if app is not None else None
        title = str(axkit.attr(window, "AXTitle") or "") if window is not None else ""
        return (str(app.localizedName()) if app is not None else ""), title
    except Exception:
        return "", ""


def _paste(text: str) -> None:
    import AppKit
    from mint.tools import fastinput
    from mint.tools import everyday as skills
    with skills._Clipboard() as clip:
        clip.board.clearContents()
        clip.board.setString_forType_(text, AppKit.NSPasteboardTypeString)
        fastinput.press_key("v", ["command"])
        time.sleep(0.15)


def edit_selection(instruction: str, replace: bool = True) -> str:
    from mint.core import llm
    from mint.tools import everyday as skills
    from mint.knowledge.skills import has_secret
    instruction = " ".join(str(instruction or "").split())
    if not instruction:
        return "FAILED: say how to change the text (e.g. 'more formal', 'fix the grammar')."
    selected = skills.get_selected_text()
    if selected.startswith(("Nothing is selected", "The selection holds no text", "Cannot read")):
        return f"FAILED: {selected} Ask the user to select the text first."
    if has_secret(selected):
        return "Refused: the selection looks like it holds a password, key or card number."
    app, title = _front()
    prompt = (f"Rewrite the text below as asked: {instruction}.\n"
              f"It is in {app or 'an app'}" + (f" (window: {title[:120]})" if title else "") + ", so keep it right for "
              "that place (an email stays an email, a chat message stays short, code stays valid code). Keep the "
              "language unless asked to translate, keep names, numbers, links and facts exactly, and keep the "
              "formatting style (plain text stays plain text; no Markdown unless the text already uses it). "
              "Return ONLY the new text - no quotes, no preamble, no explanation.\n\nTEXT:\n" + selected)
    try:
        new, _model = llm.generate(prompt)
    except Exception as error:
        return f"FAILED: could not rewrite it: {error}"
    new = new.strip("\n")
    if selected.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    if not new.strip():
        return "FAILED: the rewrite came back empty; nothing was changed."
    if not replace:
        return f"Suggested rewrite (not pasted):\n{new}"
    from mint.app import control
    if control.stopped():
        return "STOPPED by the user; nothing was changed."
    if _front() != (app, title):
        # The rewrite took a few seconds; pasting into whatever is in front now could replace the wrong text.
        return (f"The user switched away from {app or 'the app'} while I was rewriting, so nothing was pasted. "
                f"The rewrite:\n{new}\nOffer to paste it when they are back with the text selected.")
    _paste(new)
    _last.update(original=selected, new=new, app=app, at=time.time())
    from mint.tools import undo as undo_log
    undo_log.record("edit_selection", f"rewriting the selection in {app or 'the app'} ({instruction[:40]})",
                    {"kind": "edit_selection", "original": selected, "new": new, "app": app})
    return (f"Replaced the selection in {app or 'the app'} ({len(selected)} → {len(new)} characters). "
            f"New text: {new[:600]}{'…' if len(new) > 600 else ''}\n⌘Z in the app, or edit_selection action=undo, "
            "brings the original back. Tell the user briefly what changed; don't read it all out.")


def undo() -> str:
    """Paste the original back over the new text (it must still be selected)."""
    if not _last or time.time() - _last.get("at", 0) > 1800:
        return "There is no recent edit to undo. ⌘Z in the app undoes its last change."
    from mint.tools import everyday as skills
    selected = skills.get_selected_text()
    if selected.strip() != _last["new"].strip():
        if _front()[0] == _last.get("app"):
            # Still in the same app: its own undo takes the paste back.
            from mint.tools import fastinput
            fastinput.press_key("z", ["command"])
            _last.clear()
            _settled()
            return "Undid the edit with the app's own undo (⌘Z)."
        return ("The edited text is no longer selected and a different app is in front. Go back to it and "
                "press ⌘Z, or select the text and ask again.")
    _paste(_last["original"])
    _last.clear()
    _settled()
    return "Put the original text back."


def _settled() -> None:
    from mint.tools import undo as undo_log
    undo_log.settled("edit_selection")


PROMPT = """Selected text: when the user asks to change text they have selected - "make this more formal", \
"fix the grammar", "shorten this", "turn it into bullets", "translate this" - call edit_selection with the \
instruction. It rewrites and replaces the selection in place, in any app. "How would you say this better?" \
-> replace=false (show, don't paste). "Undo that" right after -> action=undo."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="edit_selection",
        description=("Rewrite the text the user has selected, in any app, and replace it in place: more formal, "
                     "friendlier, shorter, fix grammar, bullet points, translate, expand, as a reply... "
                     "action=undo puts the original back."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "instruction": types.Schema(type=S, description="how to change it, in the user's words"),
            "replace": types.Schema(type=types.Type.BOOLEAN, description="false = only suggest (default true)"),
            "action": types.Schema(type=S, enum=["edit", "undo"], description="default edit")},
            required=[]))]


def tool(args: dict) -> str:
    if str(args.get("action") or "edit").lower() == "undo":
        return undo()
    return edit_selection(str(args.get("instruction") or ""), args.get("replace") is not False)


HANDLERS = {"edit_selection": tool}
