"""App-managed preferences: settings.json, next to the mint package.

Unlike custom.json (the user's own file: accounts, aliases, routines), this one
is written by Mint whenever something is switched in the menu or the
console. Hand edits work too: the file is checked every couple of seconds and
changes apply at once, shortcuts included.

    {
      "theme": "mint",              mint | blue | aurora | sunset | rose | mono
      "position": "bottom-right",   top-right | top-left | top-center |
                                    bottom-right | bottom-left | custom
      "origin": null,               [x, top] once the panel has been dragged
      "face": true,                 the orb's eyes
      "cursor_effects": true,       sparks, ripples and highlights on screen
      "word_animation": true,       captions appear word by word
      "voice": true,                false = silent: replies are shown, not spoken
      "mic": true,                  false = microphone off; type instead
      "shortcuts": {"toggle": "cmd+j", "close": ""},
      "listen_while_working": true, still hears you mid-task
      "timeline": false,            the activity timeline (timeline.py): app, window title, page address
      "storage_folder": "",         where Mint saves what it makes; "" = ~/Documents/Mint (config.storage)
      "meeting_offer": true,        when a call starts, offer to take notes (meetings.py)
      "meeting_keep_audio": true,   keep meeting recordings (compressed) next to their notes
      "stop_words": ["stop", "cancel", ...]   said alone, these stop everything
      "voice_lock": true,           only your voice reaches Gemini (after Train my voice)
      "addressee_check": true,      ignore follow-ups said to someone else
      "vocabulary": [],             extra names and words for Mint to expect
      "assistant_name": "Mint",     what it is called; "Hey <name>" wakes it
      "user_name": "", "about_me": "",   who you are (Settings > You)
      "voice_name": "",             one of the 30 Gemini live voices; "" = Zephyr
      "speaking_style": "",         e.g. "warm and calm, a little faster"
      "hearing_fixes": [],          words Mint misheard: [{"heard": "amen", "meant": "Aman"}]
      "unload_after_minutes": 0,    >0: asleep this long, unload; Mint Ear listens. 0 = never (default)
      "input_device": "",           CoreAudio UIDs; "" follows the system default
      "output_device": "",
      "echo_cancellation": "auto",  auto (off with headphones) | on | off
      "share_mic": true,            step aside while a call or meeting uses the mic
      "share_mic_apps": [],         extra bundle ids to step aside for
      "lock_strictness": "balanced" relaxed | balanced | strict
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading

from mint.core import config

log = logging.getLogger("mint.core.prefs")

PATH = config.PROJECT_ROOT / "settings.json"

DEFAULTS: dict = {
    "theme": "mint",
    # Bottom right: at the top it sat on Chrome's profile and menu buttons.
    "position": "bottom-right",
    "origin": None,
    "face": True,
    "cursor_effects": True,
    "cute_agents": True,        # sub-agents as critters from a toy box (critters.py)
    "cute_effects": True,       # a small flourish per action, paw prints on clicks (cute_fx.py)
    "auto_emotions": True,      # the orb shows feelings on its own in conversation (moods.py)
    "word_animation": True,
    "voice": True,
    "mic": True,
    # ⌘J opens the console and closes it again; Esc closes it too. "close" is
    # empty by default: ⌘H is macOS's Hide in every app, and a global hotkey
    # would take it away from all of them. "talk" wakes Mint to listen without the
    # wake word. "dictate": hold it to dictate at the cursor (a key combo, or one
    # modifier on its own: right_option, right_command, right_control, right_shift,
    # fn), tap it twice for hands-free; "dictate_toggle" starts/stops hands-free.
    "shortcuts": {"toggle": "cmd+j", "close": "", "talk": "ctrl+option+space", "dictate": "right_option",
                  "dictate_toggle": ""},
    # Words dictation should spell exactly (names, jargon), comma-separated.
    "dictation_words": "",
    # Keep hearing the user while a task runs; "stop" (any of stop_words) cuts it off.
    "listen_while_working": True,
    # The activity timeline (timeline.py) - off until the user turns it on.
    "timeline": False,
    # Where Mint saves what it makes ("" = ~/Documents/Mint), and meeting notes.
    "storage_folder": "",
    "meeting_offer": True,
    "meeting_keep_audio": True,
    # Simple commands ("volume 30", "next song") run the moment you stop talking (instant.py).
    "instant_commands": True,
    # Voice lock: only the enrolled voice reaches Gemini (needs "Train my voice").
    "voice_lock": True,
    # Follow-ups are checked by Jev: said to Mint, or to someone else?
    "addressee_check": True,
    # Extra words and names for Mint to expect (see vocab.py).
    "vocabulary": [],
    # Who's who.
    "assistant_name": "Mint",
    "user_name": "",
    "about_me": "",
    # How Mint sounds: a Gemini live voice ("" = config.VOICE) and a free-text style.
    "voice_name": "",
    "speaking_style": "",
    # Mishearings the user corrected (hearing.py; the fix_hearing tool).
    "hearing_fixes": [],
    # Minutes asleep and idle before Mint unloads and Mint Ear listens instead
    # (mint/app/ear.py). 0 = stay loaded - the default: in the user's first hour with
    # it (27 Sep) the Ear missed wakes and the hand-over was not reliable enough.
    "unload_after_minutes": 0,
    # Audio. Devices are CoreAudio UIDs; "" follows the system default.
    "input_device": "",
    "output_device": "",
    "echo_cancellation": "auto",       # auto (off with headphones) | on | off
    "share_mic": True,                 # step aside while a call or meeting app uses the mic
    "share_mic_apps": [],              # extra bundle ids to step aside for
    # Voice lock strictness: relaxed | balanced | strict.
    "lock_strictness": "balanced",
    "stop_words": ["stop", "stop it", "stop everything", "cancel", "cancel that", "abort",
                   "enough", "hold on", "never mind", "nevermind", "shut up"],
}

THEMES = ("mint", "blue", "aurora", "sunset", "rose", "mono")
POSITIONS = ("top-right", "top-left", "top-center", "bottom-right", "bottom-left")

_lock = threading.RLock()
_values: dict = {}
_mtime = None
_listeners: list = []


def _mtime_now():
    try:
        return PATH.stat().st_mtime
    except OSError:
        return None


def _load() -> dict:
    try:
        stored = json.loads(PATH.read_text())
        return stored if isinstance(stored, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        log.warning("settings.json unreadable, keeping current settings: %s", error)
        print(f"  [settings.json is not valid JSON, ignoring the edit: {error}]", flush=True)
        return dict(_values)


def _ensure_loaded() -> None:
    global _mtime
    if _mtime is None and not _values:
        _values.update(_load())
        _mtime = _mtime_now()


def get(key: str):
    with _lock:
        _ensure_loaded()
        value = _values.get(key, DEFAULTS.get(key))
    if isinstance(value, dict) and isinstance(DEFAULTS.get(key), dict):
        return {**DEFAULTS[key], **value}
    return value


def _save() -> None:
    global _mtime
    try:
        PATH.write_text(json.dumps(_values, indent=2, ensure_ascii=False) + "\n")
        os.chmod(PATH, 0o600)
        _mtime = _mtime_now()
    except OSError as error:
        log.warning("could not save settings: %s", error)


def _notify(changes: list) -> None:
    for key, value in changes:
        for listener in list(_listeners):
            try:
                listener(key, value)
            except Exception:
                log.exception("settings listener failed")


def set(key: str, value) -> None:   # noqa: A001 - mirrors get()
    with _lock:
        _ensure_loaded()
        if key in _values and _values[key] == value:
            return
        _values[key] = value
        _save()
    _notify([(key, value)])


def name() -> str:
    """What the assistant is called (Settings ▸ You); "Mint" by default."""
    value = str(get("assistant_name") or "").strip()
    return value[:30] or "Mint"


def toggle(key: str) -> bool:
    value = not bool(get(key))
    set(key, value)
    return value


def write_full() -> None:
    """Spell every setting out in the file, so it can be edited by hand."""
    with _lock:
        _ensure_loaded()
        for key, value in DEFAULTS.items():
            _values.setdefault(key, value)
        _save()


def check_file() -> None:
    """Pick up hand edits. Cheap: one stat unless the file changed."""
    global _mtime
    mtime = _mtime_now()
    with _lock:
        if mtime is None or mtime == _mtime:
            return
        old = {k: get_raw(k) for k in set_of_keys()}
        fresh = _load()
        _values.clear()
        _values.update(fresh)
        _mtime = mtime
        changes = [(k, get_raw(k)) for k in set_of_keys() if get_raw(k) != old.get(k)]
    _notify(changes)


def get_raw(key: str):
    return _values.get(key, DEFAULTS.get(key))


def set_of_keys() -> list:
    return sorted({*DEFAULTS, *_values})


def on_change(listener) -> None:
    """listener(key, value), called on whichever thread changed the setting."""
    _listeners.append(listener)
