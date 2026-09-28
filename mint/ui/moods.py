"""Mint shows feelings on its own during a conversation, not only when asked.

Instant and local: the words themselves are read as they arrive (no model call).

* What Mint says: good news -> a smile, a joke -> a laugh, "sorry, I couldn't"
  -> a sad face, "hmm, let me think" -> thinking, "wow" -> surprised, hello or
  goodbye -> a wave, "you're welcome" -> a smile, a party -> a dance...
* What you say: thanks or praise -> it blushes, "love you" -> heart eyes,
  "haha" -> it laughs with you, a rough day -> hearts to cheer you up, being
  called useless -> it cries.
* Finishing a task: every time Mint finishes something it did for you (an app
  opened, a file written, a step done), it slides on its sunglasses - style.

At most one per turn, not more often than every few seconds, never over an
expression that is already playing or while the orb shows the task it is
doing. Off with the "auto_emotions" setting.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time

from PyObjCTools import AppHelper

from mint.core import prefs

log = logging.getLogger("mint.ui.moods")

GAP = 9.0                   # seconds between feelings shown on its own
COOL_GAP = 12.0             # sunglasses: not more often than this

# (pattern, expression, chance) - first match wins, so the strongest feelings go first.
MINT_RULES = [
    (r"\b(sorry|unfortunately|sadly|i apologi[sz]e)\b", "cry", 0.8),
    (r"\b(couldn'?t|could not|wasn'?t able|unable to|failed to|didn'?t work)\b", "cry", 0.55),
    (r"\b(ha(ha)+|lol|funny|hilarious|joke)\b|😂|🤣", "laugh", 1.0),
    (r"\b(congrat\w*|well done|great job|proud of you|you did it|bravo)\b", "clap", 1.0),
    (r"\b(love (it|that|you)|adorable|so sweet|aww+)\b|❤|💕", "love", 1.0),
    (r"\b(dance|party|celebrate|let'?s go+)\b|🎉", "dance", 0.8),
    (r"\b(wow|whoa|woah|no way|incredible|unbelievable)\b|😮", "surprised", 1.0),
    (r"\b(good night|sleep well|sweet dreams)\b", "sleepy", 1.0),
    (r"\b(hello|hi there|hey there|good (morning|afternoon|evening)|goodbye|bye|see you|welcome back)\b|👋",
     "wave", 0.9),
    (r"\b(hmm+|let me think|good question|interesting question|let me see)\b|🤔", "thinking", 0.9),
    (r"\b(just kidding|our (little )?secret|between us)\b|😉", "wink", 1.0),
    (r"\b(you('?re| are) welcome|my pleasure|happy to help|anytime|of course)\b|😊", "smile", 0.8),
    (r"\b(great|awesome|perfect|excellent|fantastic|nice|all set|done|here you go|ready)\b", "smile", 0.35),
]

USER_RULES = [
    (r"\b(stupid|dumb|useless|idiot|hate you|you suck|shut up|worst)\b", "cry", 0.9),
    (r"\b(love you|i love (it|this|that|mint))\b|❤|😍", "love", 1.0),
    (r"\b(i'?m|i am|feeling|feel) (so )?(sad|down|tired|stressed|upset|lonely|exhausted|awful)\b|"
     r"\b(bad day|rough day)\b|😢|😞", "love", 1.0),
    (r"\b(thank(s| you)|thx|ty)\b|🙏", "blush", 0.9),
    (r"\b(good job|great job|well done|nice work|you('?re| are) (the best|amazing|awesome|great|so cute|cute)|"
     r"good (girl|boy|bot)|smart)\b", "blush", 1.0),
    (r"\b(ha(ha)+|lol|lmao|rofl)\b|😂|🤣", "laugh", 1.0),
    (r"\b(wow|whoa|no way)\b", "surprised", 0.8),
    (r"\b(hello|hi|hey|good (morning|evening)|bye|good night)\b", "wave", 0.5),
]

# Tools that are bookkeeping, not a task done for the user.
QUIET = {"express", "move_orb", "set_voice", "screen_share_visibility", "find_skill", "use_skill",
         "recall_memory", "remember", "save_memory", "show_chat", "set_preference", "get_status",
         "frontmost_app", "list_open", "look", "read_window", "ui_elements", "pointer", "wait_until_done",
         "plan_task", "fix_hearing", "notes", "list_accounts"}


def enabled() -> bool:
    return prefs.get("auto_emotions") is not False


class Moods:
    def __init__(self) -> None:
        self.hud = None
        self._lock = threading.Lock()
        self._mint_text = ""
        self._user_text = ""
        self._reacted_mint = False
        self._reacted_user = False
        self._last = 0.0
        self._last_cool = 0.0
        self._did_work = False
        self._failed = False
        self._state = ""

    # --- the hooks (any thread) ------------------------------------------------------

    def heard_mint(self, text: str, new_turn: bool) -> None:
        with self._lock:
            if new_turn:
                self._mint_text, self._reacted_mint = "", False
            self._mint_text = (self._mint_text + " " + text)[-600:]
            if self._reacted_mint:
                return
            pick = _match(self._mint_text, MINT_RULES)
            if pick:
                self._reacted_mint = True
        if pick:
            self._show(pick)

    def heard_user(self, text: str, new_turn: bool) -> None:
        with self._lock:
            if new_turn:
                self._user_text, self._reacted_user = "", False
                self._mint_text, self._reacted_mint = "", False
                self._did_work = self._failed = False
            self._user_text = (self._user_text + " " + text)[-400:]
            if self._reacted_user:
                return
            pick = _match(self._user_text, USER_RULES)
            if pick:
                self._reacted_user = True
        if pick:
            self._show(pick)

    def tool_done(self, name: str, ok: bool) -> None:
        if name in QUIET:
            return
        with self._lock:
            if ok:
                self._did_work = True
            else:
                self._failed = True

    def task_complete(self) -> None:
        with self._lock:
            self._did_work, self._failed = True, False

    def state(self, state: str) -> None:
        previous, self._state = self._state, state
        # The turn is over (back to idle after working or speaking): a task done -> sunglasses.
        if state == "awake" and previous in ("working", "speaking", "thinking"):
            with self._lock:
                done = self._did_work and not self._failed
                self._did_work = self._failed = False
            if done:
                # After the orb's tick badge has faded and its face is back.
                AppHelper.callLater(1.3, self._style)

    # --- showing (main thread for the checks) --------------------------------------------

    def _free(self, now: float) -> bool:
        if not enabled():
            return False
        hud = self.hud
        if hud is None or getattr(hud, "_activity", None) is not None:
            return False                      # the orb is showing the task it is doing
        if getattr(hud, "_state", "") in ("sleeping", "paused", "offline"):
            return False
        try:
            from mint.ui.emotes import emotes
            if now - getattr(emotes, "last_played", 0.0) < 5.0:
                return False                  # one already playing (asked for, or ours)
        except Exception:
            return False
        return True

    def _show(self, name: str) -> None:
        def go():
            now = time.monotonic()
            if now - self._last < GAP or not self._free(now):
                return
            self._last = now
            from mint.ui.emotes import emotes
            print(f"  [orb feels: {name}]", flush=True)
            emotes.play(name)
        AppHelper.callAfter(go)

    def _style(self) -> None:
        now = time.monotonic()
        if now - self._last_cool < COOL_GAP or not self._free(now):
            return
        self._last_cool = self._last = now
        from mint.ui.emotes import emotes
        print("  [orb: task done - sunglasses on]", flush=True)
        emotes.play("cool")


def _match(text: str, rules) -> str | None:
    lowered = text.lower()
    for pattern, name, chance in rules:
        if re.search(pattern, lowered):
            return name if random.random() < chance else None
    return None


moods = Moods()


def attach(hud) -> None:
    """Wrap the HUD's words, state and tool hooks (once)."""
    if getattr(hud, "_moods", False):
        return
    hud._moods = True
    moods.hud = hud
    user_said, assistant_said = hud.user_said, hud.assistant_said
    set_state, activity_end, celebrate = hud.set_state, hud.activity_end, hud.celebrate

    def heard_user(text, new_turn=False):
        user_said(text, new_turn)
        try:
            moods.heard_user(str(text), new_turn)
        except Exception:
            log.debug("mood (user) failed", exc_info=True)

    def heard_mint(text, new_turn=False):
        assistant_said(text, new_turn)
        try:
            moods.heard_mint(str(text), new_turn)
        except Exception:
            log.debug("mood (mint) failed", exc_info=True)

    def state(name, note=""):
        set_state(name, note)
        try:
            moods.state(str(name))
        except Exception:
            log.debug("mood (state) failed", exc_info=True)

    def ended(name, ok):
        activity_end(name, ok)
        moods.tool_done(str(name), bool(ok))

    def celebrated():
        celebrate()
        moods.task_complete()
    hud.user_said, hud.assistant_said, hud.set_state = heard_user, heard_mint, state
    hud.activity_end, hud.celebrate = ended, celebrated
