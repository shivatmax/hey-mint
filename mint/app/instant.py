"""Instant commands: the simplest requests happen the moment the user stops talking.

"volume 30", "turn it up", "next song", "pause", "mute", "lock the screen", "dark mode on"
- a short, fixed list of whole sentences that can mean only one thing. Mint hears the
live transcript of what is being said; when the WHOLE sentence is one of these and
nothing more has been said for 0.6 s, the action runs on the Mac at once, without
waiting for the model. The model still hears the request and calls its tool a moment
later: that call is answered "already done" instead of running twice (claim()).

Anything longer or different ("pause the video in Chrome", "volume 30 and open Slack")
does not match and goes the usual way. Turn it off with the pref "instant_commands".
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

log = logging.getLogger("mint.app.instant")

QUIET = 0.6          # seconds with no new words before acting
CLAIM_FOR = 8.0      # the model's own call for the same tool within this long is a duplicate
_WORDS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
          "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "max": 100, "full": 100, "maximum": 100}
_done: list[tuple[float, str, str]] = []         # (when, tool, what was said)

_POLITE = r"(?:(?:hey )?mint[, ]*)?(?:please |can you |could you )?"
_END = r"(?: please)?[.!?]*$"


def _volume_now() -> int:
    done = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"], capture_output=True,
                          text=True, timeout=3)
    try:
        return int(done.stdout.strip())
    except ValueError:
        return 50


def _number(text: str) -> int | None:
    text = text.strip().rstrip("%").replace(" percent", "").strip()
    if text.isdigit():
        return max(0, min(100, int(text)))
    return _WORDS.get(text)


def match(said: str) -> tuple[str, dict, str] | None:
    """(tool, args, what to show) when the whole sentence is an instant command."""
    t = " ".join(str(said or "").lower().replace(",", " ").split())
    t = re.sub(r"^(?:ok(?:ay)? |so |um+ |uh+ )+", "", t)

    def full(pattern: str):
        return re.fullmatch(_POLITE + "(?:" + pattern + ")" + _END, t)

    if m := full(r"(?:set (?:the )?)?volume (?:to |at )?(\d{1,3}|[a-z ]+?)(?: ?%| percent)?"):
        level = _number(m.group(1))
        if level is not None:
            return "set_volume", {"level": level}, f"Volume {level}%"
    if full(r"(?:turn (?:it|the volume) up|volume up|louder|a bit louder|increase (?:the )?volume)"):
        return "set_volume", {"delta": 12}, "Volume up"         # the level is read when it runs (resolve)
    if full(r"(?:turn (?:it|the volume) down|volume down|quieter|a bit quieter|softer|lower (?:the )?volume)"):
        return "set_volume", {"delta": -12}, "Volume down"
    if full(r"(?:next|skip)(?: (?:song|track|this(?: song)?))?|play (?:the )?next (?:song|track)"):
        return "media_key", {"action": "next"}, "Next track"
    if full(r"previous(?: (?:song|track))?|(?:last|go back to the (?:last|previous)) (?:song|track)|"
            r"play (?:the )?previous (?:song|track)"):
        return "media_key", {"action": "previous"}, "Previous track"
    if full(r"(?:pause|stop|resume|play)(?: (?:the )?(?:music|song|track|it))?|(?:pause|play) music"):
        if not t.startswith(("stop",)) or "music" in t or "song" in t:     # a bare "stop" stops Mint, not music
            return "media_key", {"action": "playpause"}, "Play/pause"
    if full(r"(?:brighter|(?:turn (?:the )?)?brightness up|increase (?:the )?brightness|(?:make (?:the |my )?)?screen brighter)"):
        return "mac", {"control": "brightness", "value": "up"}, "Brighter"
    if full(r"(?:dimmer|dim (?:the |my )?screen|(?:turn (?:the )?)?brightness down|decrease (?:the )?brightness|"
            r"lower (?:the )?brightness)"):
        return "mac", {"control": "brightness", "value": "down"}, "Dimmer"
    if m := full(r"(?:set (?:the )?)?brightness (?:to |at )?(\d{1,3}|[a-z ]+?)(?: ?%| percent)?"):
        level = _number(m.group(1))
        if level is not None:
            return "mac", {"control": "brightness", "value": str(level)}, f"Brightness {level}%"
    if full(r"(?:mute|mute (?:the )?(?:sound|volume|mac|audio))"):
        return "system_action", {"action": "mute"}, "Muted"
    if full(r"(?:unmute|unmute (?:the )?(?:sound|volume|mac|audio))"):
        return "system_action", {"action": "unmute"}, "Unmuted"
    if full(r"lock (?:the |my )?(?:screen|mac|computer)"):
        return "system_action", {"action": "lock"}, "Locking the screen"
    if full(r"(?:turn )?(?:on )?dark mode(?: on)?|switch to dark mode"):
        return "system_action", {"action": "dark_mode_on"}, "Dark mode on"
    if full(r"(?:turn )?(?:off )?dark mode off|light mode(?: on)?|switch to light mode|turn off dark mode"):
        return "system_action", {"action": "dark_mode_off"}, "Dark mode off"
    if full(r"show (?:me )?(?:the |my )?desktop"):
        return "mac", {"control": "show", "value": "desktop"}, "Desktop"
    if full(r"(?:open |show )?mission control"):
        return "mac", {"control": "show", "value": "mission_control"}, "Mission Control"
    if full(r"(?:keep (?:the |my )?(?:mac|computer|screen) (?:awake|on)|stay awake|don'?t (?:go to )?sleep|"
            r"caffeinate)"):
        return "mac", {"control": "keep_awake", "value": "on"}, "Staying awake"
    for words, layout, note in ((r"left(?: half)?", "left_half", "Left half"),
                                (r"right(?: half)?", "right_half", "Right half")):
        if full(rf"(?:snap|move|put) (?:this|the|it)?(?: window)? ?(?:to |on )?(?:the )?{words}"
                rf"|(?:this|the) window (?:to |on )?(?:the )?{words}"):
            return "mac", {"control": "window", "value": layout}, note
    if full(r"maximi[sz]e(?: (?:this|the|it))?(?: window)?"):
        return "mac", {"control": "window", "value": "maximize"}, "Maximised"
    if full(r"(?:make (?:this|it) |go )?full ?screen(?: (?:this|it|the window))?|enter full ?screen"):
        return "mac", {"control": "window", "value": "fullscreen"}, "Full screen"
    if full(r"(?:take a |a |quick )?screenshot(?: (?:this|it|now|please))?|snap (?:it|this|the screen)|"
            r"(?:click|grab) (?:a )?screenshot"):
        return "screenshot", {"what": "screen"}, "Screenshot"
    if full(r"(?:start )?(?:recording|record) (?:my |the )?(?:whole )?screen|start (?:a )?screen recording|"
            r"start (?:a )?video recording"):
        return "screen_record", {"action": "start", "target": "screen"}, "Recording the screen"
    if full(r"stop (?:the )?(?:screen|video) recording|stop recording (?:the |my )?screen"):
        return "screen_record", {"action": "stop"}, "Stopping the recording"
    return None


def enabled() -> bool:
    from mint.core import prefs
    value = prefs.get("instant_commands")
    return True if value is None else bool(value)


_model_calls: list[tuple[float, str]] = []     # tools the model itself ran (so instant never repeats one)


def resolve(tool: str, args: dict) -> dict:
    """Relative volume becomes a level - read now, off the event loop."""
    if tool == "set_volume" and "delta" in args:
        return {"level": max(0, min(100, _volume_now() + int(args["delta"])))}
    return args


def ran(tool: str, args: dict, said: str) -> None:
    _done.append((time.monotonic(), tool, dict(args), said))
    del _done[:-10]


def model_ran(tool: str) -> None:
    _model_calls.append((time.monotonic(), tool))
    del _model_calls[:-20]


def model_just_ran(tool: str, within: float = 6.0) -> bool:
    """The model already ran this tool for the sentence (its call can come before the transcript settles)."""
    now = time.monotonic()
    return any(t == tool and now - when < within for when, t in _model_calls)


def _same(tool: str, a: dict, b: dict) -> bool:
    if tool == "set_volume":
        try:
            return abs(int(a.get("level", -99)) - int(b.get("level", 99))) <= 3
        except (TypeError, ValueError):
            return False
    if tool == "mac":
        if str(a.get("control", "")).lower() != str(b.get("control", "")).lower():
            return False
        va, vb = str(a.get("value", "")).lower(), str(b.get("value", "")).lower()
        # "brighter" vs the model's "up" are the same request; two different numbers are not, nor two layouts.
        return va == vb or (str(a.get("control")).lower() == "brightness" and not (va.isdigit() and vb.isdigit()))
    return str(a.get("action", "")).lower() == str(b.get("action", "")).lower()


def claim(tool: str, args: dict | None = None) -> str | None:
    """The model asked for `tool`: if the same thing was just done instantly, say so (once) instead of
    doing it twice. A different request ("now make it 50") runs as usual."""
    now = time.monotonic()
    for i, (when, done_tool, done_args, said) in enumerate(_done):
        if done_tool == tool and now - when < CLAIM_FOR and (args is None or _same(tool, done_args, args or {})):
            del _done[i]
            return (f"Already done instantly the moment the user finished saying '{said}'. Do not do it again; "
                    "just confirm in two or three words.")
    return None
