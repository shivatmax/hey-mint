"""Words Mint mishears, and what the user actually said - learned as it goes.

Taught by the fix_hearing tool: the user corrects Mint ("I said Aman, not
Amen", "it's Hey Mint, not payment") and the model saves the pair. Stored in
settings.json "hearing_fixes" (hand edits work too):

    [{"heard": "amen", "meant": "Aman", "added": "2026-09-27", "used": 3}]

A fix is used three ways:
1. apply() rewrites the user's words as they arrive, before they are shown,
   remembered or checked (goodbye, stop, talking-to-me).
2. prompt_text() lists them in the instructions (at the next connect), so the
   model reads the mishearing as the right word.
3. vocab.terms() adds the right words to the transcription's custom
   vocabulary, which biases the recogniser itself.

The wake phrase is special. It is recognised on the Mac and cut from the
audio Gemini gets (wake.phrase_cut); if a scrap of it still leads a turn -
"payment", "hymen", "Hey man" in the 26-27 Sep logs - it is dropped there and
nowhere else: "payment" later in a sentence is a real payment.
"""

from __future__ import annotations

import re
import time

from . import prefs

KEY = "hearing_fixes"
LIMIT = 60
# How "Hey Mint" came back from Gemini's transcription in the user's logs.
# Not "Amen" or "Hemant": the user's friend Aman, and a real name, can lead a turn.
WAKE_LOOKALIKES = ("payment", "payments", "a payment", "hymen", "hey man", "hey men",
                   "hey mint", "hi mint", "hey, mint", "hi, mint")


def _clean(text: str) -> str:
    return " ".join(str(text or "").split()).strip(" .,!?:;\"'")


def fixes() -> list[dict]:
    value = prefs.get(KEY) or []
    return [f for f in value if isinstance(f, dict) and _clean(f.get("heard")) and _clean(f.get("meant"))]


def _wake_phrases() -> set[str]:
    name = prefs.name().lower()
    return {f"hey {name}", f"hi {name}", f"hey, {name}", f"hi, {name}", name}


def _is_wake(meant: str) -> bool:
    return _clean(meant).lower() in _wake_phrases()


def teach(heard: str, meant: str, forget: bool = False) -> str:
    """Save (or drop) a correction. The fix_hearing tool."""
    heard, meant = _clean(heard), _clean(meant)
    if not heard:
        return "Say which word was misheard (heard) and what the user said (meant)."
    current = fixes()
    kept = [f for f in current if _clean(f["heard"]).lower() != heard.lower()]
    if forget:
        if len(kept) == len(current):
            return f"There was no saved correction for '{heard}'."
        prefs.set(KEY, kept)
        print(f"  [hearing: forgot '{heard}']", flush=True)
        return f"Forgot the correction for '{heard}'."
    if not meant or meant.lower() == heard.lower():
        return "The misheard word and the right one must differ."
    if len(heard) > 60 or len(meant) > 60:
        return "Keep it to the word or short phrase that was misheard."
    kept.append({"heard": heard, "meant": meant, "added": time.strftime("%Y-%m-%d"), "used": 0})
    prefs.set(KEY, kept[-LIMIT:])
    print(f"  [hearing: '{heard}' means '{meant}']", flush=True)
    where = ("at the start of what the user says (a wake word the Mac already caught)" if _is_wake(meant)
             else "wherever it comes up")
    return (f"Saved: '{heard}' now means '{meant}', {where}. It fixes the user's words on screen and in "
            "memory from now on, and the recogniser expects the right word from the next session. Carry on "
            f"with what they asked, reading '{heard}' as '{meant}'.")


def _pattern(word: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in word.split()) + r"(?!\w)", re.I)


def strip_wake(text: str) -> tuple[str, bool]:
    """Drop a leading scrap of the wake phrase. -> (text, dropped)"""
    lead = [w for w in WAKE_LOOKALIKES] + [_clean(f["heard"]) for f in fixes() if _is_wake(f["meant"])]
    for word in sorted(set(lead), key=len, reverse=True):
        match = re.match(r"\s*" + _pattern(word).pattern + r"[\s,.!?]*", text, re.I)
        if match:
            return text[match.end():], True
    return text, False


def apply(text: str) -> str:
    """The user's words with learned mishearings put right (not the wake phrase:
    see strip_wake)."""
    if not text:
        return text
    changed = False
    for fix in fixes():
        if _is_wake(fix["meant"]):
            continue
        new = _pattern(_clean(fix["heard"])).sub(_clean(fix["meant"]), text)
        if new != text:
            text, changed = new, True
            fix["used"] = int(fix.get("used", 0)) + 1
    if changed:
        _count_later()
    return text


_dirty = [0.0]


def _count_later() -> None:
    """Usage counts are nice to have; write them at most once a minute."""
    if time.monotonic() - _dirty[0] > 60:
        _dirty[0] = time.monotonic()
        prefs.set(KEY, [dict(f) for f in fixes()])


def meant_words() -> list[str]:
    return [_clean(f["meant"]) for f in fixes() if not _is_wake(f["meant"])]


def prompt_text() -> str:
    name = prefs.name()
    lines = [f"The wake phrase \"Hey {name}\" is caught on the Mac and cut from the audio you get, so a "
             "turn normally starts with the request itself. If a turn still starts with a stray word that "
             f"sounds like it - 'payment', 'hymen', 'Hey man' - that is the wake phrase, not a request: "
             "ignore it, and if nothing else was said, just ask briefly what they need."]
    pairs = [f for f in fixes() if not _is_wake(f["meant"])]
    if pairs:
        lines.append("Words you have misheard before, and what the user actually says: "
                     + "; ".join(f"'{_clean(f['heard'])}' is '{_clean(f['meant'])}'" for f in pairs) + ".")
    lines.append("When the user corrects how you heard a word or name (\"I said Aman, not Amen\", "
                 f"\"it's Hey {name}, not payment\"), call fix_hearing once with what you heard and what "
                 "they meant, then carry on with the corrected request. Not for a change of mind.")
    return "\n".join(lines)


def declarations():
    from google.genai import types
    return [types.FunctionDeclaration(
        name="fix_hearing",
        description=("Remember a word or name you misheard, so it is heard right from now on. Call it when "
                     "the user corrects your hearing (\"I said Aman, not Amen\"; \"it's Hey Mint, not "
                     "payment\"; \"no, KubeCon\"). forget=true removes a saved correction."),
        parameters=types.Schema(type="OBJECT", properties={
            "heard": types.Schema(type="STRING", description="What you heard (the wrong word or phrase)."),
            "meant": types.Schema(type="STRING", description="What the user actually said."),
            "forget": types.Schema(type="BOOLEAN", description="Remove the saved correction for `heard`."),
        }, required=["heard"]))]


HANDLERS = {
    "fix_hearing": lambda a: teach(str(a.get("heard", "")), str(a.get("meant", "")),
                                   str(a.get("forget", "")).lower() in ("true", "1", "yes")),
}
