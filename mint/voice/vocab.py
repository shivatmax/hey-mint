"""Your words: names, channels, jargon and corrections, so Mint hears them right.

Gathered from what already describes you - custom.json (accounts, Slack
workspaces and channels, aliases, routines), settings.json "vocabulary", and
corrections Mint has been taught ("no, I said on-call") which are saved as
remembered facts under the topic "vocabulary".

Used twice per session: as custom_vocabulary for the live transcription (the
captions, the "stop" check and the talking-to-me check read it), and in the
instructions, so the model itself expects these words.
"""

from __future__ import annotations

import re

from mint.core import custom
from mint.core import prefs

LIMIT = 100          # the transcription API does best with up to ~100 terms
_common: set[str] | None = None


def _is_common(term: str) -> bool:
    """A plain dictionary word ("work", "experts") needs no biasing."""
    global _common
    if _common is None:
        try:
            with open("/usr/share/dict/words") as f:
                _common = {w.strip().lower() for w in f if w.strip().islower()}
        except OSError:
            _common = set()
    words = term.split()
    return all(w.islower() and w in _common for w in words)


def _clean(term: str) -> str:
    return " ".join(str(term).replace("_", " ").split()).strip(" .,:;")


def terms() -> list[str]:
    settings = custom.get()
    found: list[str] = []

    def add(value) -> None:
        if isinstance(value, str):
            value = _clean(value)
            # Addresses are said as words; the domain and name parts are what matter.
            if "@" in value:
                name, _, domain = value.partition("@")
                add(name.replace(".", " "))
                add(domain.split(".")[0])
                return
            if (2 <= len(value) <= 40 and not _is_common(value)
                    and value.lower() not in {t.lower() for t in found}):
                found.append(value)

    about = settings.get("about_me") or ""
    for name in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", about):
        if name not in {"The", "Keep", "My", "Please"}:
            add(name)
    for word in prefs.get("vocabulary") or []:
        add(word)
    for key in ("slack_workspaces", "slack_channels", "accounts", "routines"):
        for name, value in (settings.get(key) or {}).items():
            add(name)
            if isinstance(value, str):
                add(value)
    for heard, meant in (settings.get("aliases") or {}).items():
        add(meant)
    for pair in corrections():
        add(pair[1])
    from mint.voice import hearing
    for word in hearing.meant_words():
        add(word)
    name = prefs.name()
    add(f"Hey {name}")   # "Hey Mint" was heard as "Hemant" (a common name) without this
    add(name)
    try:
        from mint.voice import wake
        for phrase in wake.active_phrases():     # a wake phrase of the user's own ("Hey Jarvis")
            add(phrase)
    except Exception:
        pass
    if user := str(prefs.get("user_name") or "").strip():
        add(user)
    global _common
    _common = None              # the 235,000-word dictionary is only for this: 19 MB kept for nothing (6 Oct)
    return found[:LIMIT]


def corrections() -> list[tuple[str, str]]:
    """[(what was heard, what was meant)] from remembered vocabulary facts."""
    try:
        from mint.knowledge import notes
        lines = notes.prompt_text().splitlines()
    except Exception:
        return []
    pairs = []
    for line in lines:
        if "[vocabulary]" not in line.lower():
            continue
        match = re.search(r"[\"“']([^\"”']+)[\"”']\s+(?:means|is|=)\s+[\"“']?([^\"”'(]+)", line)
        if match:
            pairs.append((_clean(match.group(1)), _clean(match.group(2))))
    return pairs


def prompt_text() -> str:
    from mint.voice import hearing
    words = terms()
    fixes = corrections()
    parts = [hearing.prompt_text()]
    from mint.tools.diet import clip
    if words:
        parts.append(clip("Words and names this user says often - when you hear something close to one "
                          "of these, it is almost certainly that: " + ", ".join(words) + ".", 400))
    if fixes:
        parts.append(clip("Mishearings you have been corrected on: " +
                          "; ".join(f"'{h}' means {m}" for h, m in fixes) + ".", 300))
    return "\n".join(parts)
