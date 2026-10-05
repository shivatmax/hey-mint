"""When Mint listens, and whether what it heard was meant for it.

Two decisions, kept free of audio and the network so they can be tested with
plain words and times (publish/overlay/tests/test_listening.py):

1. The window (Window). The wake word opens it. Once Mint has finished - done
   speaking, no tool running, its turn over - it listens only `follow_up_seconds`
   (a pref, 6 s) for a real follow-up, then sleeps and needs the wake word again.
   Only a turn judged to be for Mint keeps it open; talk around the Mac does not.
   Before this (to 5 Oct) every transcribed scrap and every sound restarted a 12 s
   quiet clock, so Mint stayed open for minutes: 1 Oct 20:56-20:59 it listened
   through Hindi chat, swearing, music and "payment" and answered half of it.

2. The barrier (quick, judge, fallback). Before Mint acts on what it heard, is it
   for Mint and does it ask for something? Free rules first (stop words, its name,
   "can you ...", an answer to Mint's own question); otherwise Jev decides in about
   0.4 s, and the model's reply waits for that. Turns the wake word opened are
   trusted unless they are a stray scrap ("Generally parents", "Alex Lake" just
   after a wake). Follow-ups must look like a request: when unsure, Mint does not
   act. Real follow-ups it answered before this, from the log: "enter Vadodara"
   ("Did you need help with anything else?"), "hammering", "option hoodie jacket"
   (a web search!), "Barf nahi hai na?" ("Haan, barf nahi"), "Siri, uncle Siri.".
"""

from __future__ import annotations

import math
import re

FOLLOW_UP = 6.0       # seconds Mint keeps listening after it is done (pref follow_up_seconds)
WAKE_GRACE = 8.0      # after the wake word: time to start saying the request
END_GAP = 1.0         # quiet that ends an utterance, for the window
JUDGE_WAIT = 8.0      # an utterance begun in time waits this long for its verdict (transcripts lag)
MAX_OVERRUN = 20.0    # never listen longer than this past the window on speech alone
STALE = 30.0          # a request "being answered" with no word from the server this long is over

# Mint's last words asked the user something: a short reply is the answer.
ASKS = re.compile(r"\?\s*$|\b(should i|shall i|do you want|want me to|would you like|which one|or should)\b", re.I)


def follow_up_seconds() -> float:
    """The follow-up window from settings: 0 = sleep as soon as Mint is done."""
    from mint.core import prefs
    try:
        value = float(prefs.get("follow_up_seconds"))
    except (TypeError, ValueError):
        value = FOLLOW_UP
    return max(0.0, min(30.0, value))


# --- the window ---------------------------------------------------------------------------------------

class Window:
    """How long Mint keeps listening without the wake word.

    `until` is when it stops (monotonic seconds); infinite while a request for
    Mint is being answered. The session feeds it what happens - woke(), sound()
    for every microphone frame, judged() for every verdict, finished() when Mint
    is done - and asks should_sleep() twice a second. Speech that began inside
    the window may finish and be judged; if it was not for Mint, the window is
    not extended and Mint sleeps."""

    def __init__(self, follow_up: float = FOLLOW_UP, wake_grace: float = WAKE_GRACE) -> None:
        self.follow_up = follow_up
        self.wake_grace = wake_grace
        self.until = 0.0
        self._after = "the wake word"         # what opened the window (for the log line)
        self._began: float | None = None     # when the utterance going on now began
        self._sound_at = 0.0                  # its last loud moment
        self._in_time: float | None = None    # last sound of an unjudged utterance that began in the window

    def woke(self, now: float) -> None:
        self.until = now + max(self.wake_grace, self.follow_up)
        self._after = "the wake word"
        self._began = self._in_time = None

    def handling(self) -> None:
        """A request for Mint is being answered: listen until it is done."""
        self.until = math.inf

    def finished(self, now: float) -> None:
        """Mint is done (spoke its reply, finished its work): the follow-up window starts now."""
        self.until = now + self.follow_up
        self._after = "the reply"

    def why(self) -> str:
        """For "[asleep: ...]" in the log."""
        if self._after == "the wake word":
            return f"nothing said after the wake word ({max(self.wake_grace, self.follow_up):g}s)"
        return f"no follow-up in {self.follow_up:g}s"

    def sound(self, now: float, speaking: bool) -> None:
        """One microphone frame: the user's speech or not (the voice lock decides whose it is)."""
        if not speaking:
            return
        if self._began is None or now - self._sound_at > END_GAP:
            self._began = now
        self._sound_at = now
        if self._began <= self.until:
            self._in_time = now

    def other_voice(self) -> None:
        """The voice lock decided the speech going on is someone else's: it holds nothing open."""
        self._in_time = None
        self._began = None

    def judged(self, now: float, for_mint: bool) -> None:
        """The words heard were judged. Only a request for Mint extends the window; whatever
        is still being said after a verdict counts as new speech."""
        self._in_time = None
        self._began = None
        if for_mint:
            self.handling()

    def waiting(self, now: float) -> bool:
        """Speech that began inside the window is still going on, or waits for its verdict."""
        return (self._in_time is not None and now - self._in_time < JUDGE_WAIT
                and now - self.until < MAX_OVERRUN)

    def should_sleep(self, now: float, busy: bool = False, playing: bool = False, deciding: bool = False,
                     idle_for: float = 0.0) -> bool:
        """busy: a tool runs; playing: Mint speaks; deciding: words wait for their verdict;
        idle_for: seconds since the server or a tool last did anything."""
        if busy or playing or deciding:
            return False
        if self.until == math.inf:
            if idle_for < STALE:
                return False
            self.finished(now - idle_for)    # the answer never finished (a dropped session): over
        return now > self.until and not self.waiting(now)


# --- the barrier --------------------------------------------------------------------------------------

_FILLERS = {"okay", "ok", "so", "and", "also", "now", "then", "just", "please", "yes", "yeah", "no", "hey", "hi",
            "actually", "alright", "well", "um", "umm", "uh", "oh", "right", "fine", "acha", "achha", "haan",
            "again", "first", "quickly", "kindly", "but", "sorry", "wait", "listen", "boss"}
# A sentence that starts (after fillers) with one of these is a command.
_VERBS = {"open", "close", "quit", "show", "play", "pause", "resume", "skip", "search", "find", "look", "check",
          "tell", "give", "send", "reply", "write", "type", "read", "make", "create", "generate", "draw", "set",
          "turn", "switch", "go", "move", "come", "take", "put", "copy", "paste", "delete", "remove", "clear",
          "empty", "add", "save", "download", "install", "uninstall", "zip", "share", "call", "text", "email",
          "message", "translate", "summarize", "summarise", "explain", "remind", "schedule", "book", "start",
          "restart", "run", "launch", "select", "click", "scroll", "mute", "unmute", "lock", "increase",
          "decrease", "raise", "lower", "change", "rename", "upload", "record", "capture", "teach", "learn",
          "help", "continue", "repeat", "undo", "redo", "minimize", "minimise", "maximize", "hide", "fix",
          "sort", "organize", "organise", "tidy", "convert", "compress", "list", "print", "connect",
          "disconnect", "enable", "disable", "update", "refresh", "keep", "let's", "lets", "bring", "use", "try",
          "do", "drop", "fly", "circle", "dim", "brighten", "join", "leave", "finish", "complete", "note",
          "describe", "compare", "calculate", "convert", "answer", "ask", "watch", "listen", "stop", "cancel"}
_ASK_YOU = re.compile(
    r"\b(can|could|would|will|did|do|are|have|were) you\b|\bi (want|wanted|need|would like|'d like)( you)? to\b|"
    r"\bplease\b|\bdo one thing\b|\b(tell|show|give|send) me\b|\bfor me\b", re.I)
_QUESTION = re.compile(r"^(what|what's|whats|where|where's|when|which|who|who's|how|how's|why|is|are|does|did|"
                       r"can|could|will|would|should|do)\b", re.I)
# Short replies to a question Mint asked.
_ANSWERS = {"yes", "yeah", "yep", "yup", "no", "nope", "nah", "haan", "han", "ha", "nahi", "nahin", "ok", "okay",
            "sure", "the", "that", "this", "first", "second", "third", "last", "both", "neither", "either", "one",
            "two", "three", "it", "same", "go", "do", "don't", "dont", "please", "correct", "right", "wrong",
            "exactly", "absolutely", "definitely", "never", "not", "only", "all", "none", "ji", "theek"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower())


def _latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if c.isascii()) / len(letters) > 0.7


def addressed(text: str, name: str = "Mint") -> bool:
    """The words call Mint by name, or start with a scrap of the wake phrase ("payment")."""
    from mint.voice import hearing
    if re.search(rf"\b{re.escape(name.lower())}\b", (text or "").lower()):
        return True
    try:
        return hearing.strip_wake(text or "")[1]
    except Exception:
        return False


def mint_asked(mint_said: str) -> bool:
    return bool(ASKS.search(" ".join((mint_said or "").split())))


def request_shape(text: str) -> bool:
    """English that reads as a command or a request to Mint: "open Slack", "just go back to the
    notch", "can you ...", "I want you to ...". Free - no model call."""
    if not _latin(text):
        return False
    if _ASK_YOU.search(text):
        return True
    words = [w for w in _words(text) if w not in _FILLERS]
    return any(w in _VERBS for w in words[:3])


# A sentence that stops on one of these isn't finished: the user is still thinking ("I want to do", "can you").
_DANGLING = {"to", "the", "a", "an", "and", "or", "but", "of", "for", "with", "in", "on", "at", "into", "from",
             "my", "your", "his", "her", "our", "their", "its", "some", "any",
             "want", "wanna", "gonna", "need", "do", "does", "is", "are", "was", "were", "be", "can", "could",
             "would", "will", "should", "shall", "may", "might", "must", "i", "we", "so", "like",
             "um", "umm", "uh", "uhh", "hmm", "er", "erm", "basically", "about", "if", "because", "then", "also",
             "just", "let", "let's", "lets", "maybe", "actually", "which", "what", "where", "when", "how", "who",
             "very", "more", "most", "kind", "sort", "going", "trying"}


def unfinished(text: str, mint_said: str = "") -> bool:
    """The words stop mid-sentence ("I want to do", "can you", "so basically the") or are only fillers ("um",
    "okay so"): the user is still thinking. Mint waits - it doesn't answer "your request got cut off". An answer
    to Mint's own question ("the second", "yes") is never unfinished; words in other scripts are for Jev."""
    words = _words(text)
    if not words:
        return not (text or "").strip() or _latin(text)
    if mint_asked(mint_said) and len(words) <= 8:
        return False
    if words[-1] in _DANGLING or re.search(r"\b(can|could|would|will|do|did|are|have|should) you$",
                                           " ".join(words)):
        return True
    return not [w for w in words if w not in _FILLERS]          # "okay so", "um", "hey"


def scrap(text: str) -> bool:
    """One stray word that is no command, question or answer ("ma'am", "Friday", "hammering")."""
    words = _words(text)
    meaningful = [w for w in words if w not in _FILLERS]
    return len(meaningful) == 1 and len(words) <= 2 and meaningful[0] not in _VERBS and \
        not _QUESTION.match(meaningful[0]) and meaningful[0] not in _ANSWERS


def quick(text: str, kind: str, mint_said: str = "", name: str = "Mint", special: bool = False) -> str | None:
    """A verdict without a model call: "act", "ignore", "wait" (unfinished: say nothing yet), or None (ask Jev).

    kind: "asked" (the user opened Mint on purpose: shortcut, menu, typed), "wake" (the first
    thing said after the wake word) or "follow" (anything else: follow-ups, talk while Mint
    works, after Mint woke itself for news). special: stop words, a goodbye, a yes/no to the
    guard, an instant command, a lesson or demonstration under way - always let through."""
    words = _words(text)
    if not words and not text.strip():
        return None                               # nothing transcribed yet
    if kind == "asked" or special:
        return "act"
    if unfinished(text, mint_said):
        return "wait"                             # still thinking: say nothing, keep listening for the rest
    if kind == "wake" and scrap(text) and not addressed(text, name):
        return "wait"                             # "Hey Mint ... ma'am": nothing to answer yet

    if addressed(text, name):
        return "act"
    if kind == "wake":
        # Said right after "Hey Mint": trusted, unless it is a short scrap with no request in it.
        if len(words) > 3 or request_shape(text) or _QUESTION.match(" ".join(words)):
            return "act"
        return None
    if mint_asked(mint_said) and words and (words[0] in _ANSWERS or len(words) <= 3) and len(words) <= 8:
        return "act"                              # the answer to Mint's own question
    if request_shape(text):
        return "act"
    return None


_COMMON = {"that", "this", "with", "have", "there", "here", "what", "your", "from", "they", "them", "then",
           "just", "about", "would", "could", "should", "will", "been", "were", "it's", "i've", "i'm", "boss",
           "okay", "done", "now", "please", "want", "like", "some", "into", "back", "also", "again", "for", "you"}


def echoes(text: str, mint_said: str) -> bool:
    """The words pick up a word from Mint's last reply ("I've opened the GIF" - "it's not opened in
    Safari"): most likely a reply to Mint, about what it just did."""
    if not _latin(text):
        return False
    said = {w for w in _words(mint_said) if len(w) >= 4 and w not in _COMMON}
    return bool(said & {w for w in _words(text) if len(w) >= 4 and w not in _COMMON})


def judge(kind: str, pick: str | None, probability: float, mint_said: str = "", text: str = "") -> str:
    """Jev's verdict ("mint" / "other" / "scrap", with its probability) as "act" or "ignore".
    After a wake word only a sure "not for Mint" is ignored; in a follow-up only a sure
    "for Mint" acts - looser when Mint just asked something or the words are about its reply."""
    if kind != "follow":
        return "ignore" if pick in ("other", "scrap") and probability >= 0.6 else "act"
    if text and echoes(text, mint_said) and not (pick in ("other", "scrap") and probability >= 0.75):
        return "act"
    need = 0.4 if mint_asked(mint_said) else 0.55
    return "act" if pick == "mint" and probability >= need else "ignore"


def fallback(kind: str, text: str, mint_said: str = "") -> str:
    """No verdict from Jev (no key, offline, too slow). After the wake word: act. A follow-up acts
    only if it looks like a question or request to someone listening."""
    if kind != "follow":
        return "act"
    words = _words(text)
    if mint_asked(mint_said) and 0 < len(words) <= 8:
        return "act"
    if not _latin(text) or len(words) < 3:
        return "ignore"
    if _QUESTION.match(" ".join(words)) or {"you", "your", "me", "my"} & set(words) or echoes(text, mint_said):
        return "act"
    return "ignore"


OPTIONS = {
    "mint": "Said TO Mint, the voice assistant: asks it to do, open, find, check, play, send, change or stop "
            "something; asks it a question; or answers / reacts to what Mint just said or asked.",
    "other": "NOT said to Mint: talk with another person in the room or on a call (often Hindi or Hinglish chat), "
             "a name called out to someone, a remark, swearing, a video or music playing, reading aloud.",
    "scrap": "A scrap with nothing to act on: one or two stray words, a half sentence that trails off, a bare "
             "noun or place, or a misheard noise.",
}
INSTRUCTIONS = (
    "A voice assistant called Mint hears everything in a room: the user, other people, calls, videos. `request` "
    "is the speech-to-text of what it just heard (can be misheard). `mint_said` is Mint's last reply; `moment` is "
    "'woken by name' (the user just said Hey Mint) or 'follow-up' (Mint finished and is only listening a few more "
    "seconds, or is busy working). Pick mint ONLY when the words clearly ask Mint to do something (a command: open, "
    "play, show, go, move, make, find, send, check, stop...), ask it a question, or directly answer what mint_said "
    "asked or reply about what it just did. Pick other for talk aimed at people: Hindi or Hinglish conversation, "
    "calling someone's name, remarks, swearing, a call or video. Pick scrap for stray words or a bare noun or place "
    "with no verb that answers nothing Mint asked. During a follow-up, when unsure, it is NOT mint.")


def ask_jev(text: str, kind: str, mint_said: str = "", voice: str = "normal", name: str = "Mint",
            timeout: float = 3.0) -> tuple[str | None, float, float] | None:
    """Jev's pick for `text`: (option, its probability, confidence), or None when Jev is unreachable.
    Blocking (about 0.4 s); call it in a thread."""
    from mint.core import jev
    def named(s: str) -> str:
        return s.replace("Mint", name) if name != "Mint" else s
    answers = jev.ask({"request": " ".join(text.split()), "mint_said": (mint_said or "")[-300:],
                       "moment": "woken by name" if kind == "wake" else "follow-up", "voice": voice},
                      {"pick": {"type": "choice", "instructions": named(INSTRUCTIONS),
                                "criteria": {k: named(v) for k, v in OPTIONS.items()}}}, timeout=timeout)
    pick = (answers or {}).get("pick")
    if not pick:
        return None
    chosen = pick.get("choice")
    try:
        probability = float((pick.get("probabilities") or {}).get(chosen, 0.0))
        confidence = float(pick.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    return (None if chosen == "none" else chosen), probability, confidence
