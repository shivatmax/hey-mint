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
      "follow_up_seconds": 6,       after a reply, listen this long for a follow-up; then the wake word again
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
      "personality": "",            up to 100 characters: "funny and a bit sarcastic"; "" = default
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
    "notch_mode": False,        # Mint lives in the camera notch like a Dynamic Island (notch.py)
    "notch_words": True,        # notch mode: the words show in the notch as they are spoken
    "notch_controls": True,     # notch mode: controls when the pointer rests on the notch
    "notch_idle_face": True,    # notch mode: the little Mint beside the camera when idle (off: a plain notch)
    "notch_playful": True,      # notch mode: bouncy springs, hops, breathing with the voice
    "notch_music": True,        # notch mode: what's playing in the notch (artwork, bars, the player on hover)
    "notch_shelf": True,        # notch mode: a file shelf (drag files to the notch; AirDrop, share)
    "notch_calendar": True,     # notch mode: the week and today's events in the open notch
    "notch_battery": True,      # notch mode: battery badge, and a peek when plugged in or unplugged
    "notch_search": True,       # notch mode: Spotlight in the notch - files and apps, grids, drag out
    # The character system (plans/grokbot-notch-overhaul.md)
    "status_badge": True,       # a little badge on Mint's face: dots while working, red on error, green when done
    "poke_play": True,          # poke Mint: a slap, three quick ones make it dizzy, keep going and it gets annoyed
    "greeting": True,           # a sparkly hello when Mint wakes up for the day
    "window_glow": True,        # a glowing border round the window Mint is working in
    "notch_composer": True,
    "notch_agents_bar": True,   # notch mode: a slim bar under the notch with what your coding agents are doing     # notch mode: type to Mint right in the notch
    "ui_sounds": True,          # soft little sounds for open, done, error, pokes
    "sound_volume": 0.7,        # how loud those sounds are, 0..1
    "sounds_notch": True,       # sounds: the notch opening and closing
    "sounds_tasks": True,       # sounds: a task done or failed, a message sent, a file dropped
    "sounds_play": True,        # sounds: pokes, dizzy, the morning hello
    "wander": True,             # the orb takes a little trip near home now and then (motion.py)
    "motion": "full",           # animation: full (bouncy, playful) / calm (gentle, fewer extras) / minimal (fades only)
    "notch_open_to": "auto",    # notch mode: the tab the open notch shows first - auto / home / search / shelf / agents
    "menubar_face": True,       # the menu bar icon is Mint's face and shows its state
    # Claude mode (notch_agents): Claude Code and Codex sessions live in the notch. auto / on / off.
    "agent_mode": "auto",
    "agent_approvals": True,    # an agent's permission request or question opens the notch on it
    "agent_open_on_done": True, # a finished agent's summary shows in the notch for a few seconds
    "agent_compact": False,     # the Agents tab minimized to one line (the minimize button in the pane)
    "agent_telegram": "away",   # coding agents message you on Telegram: away (from the Mac) / always / off
    "agent_checks": True,       # read agents' test runs, risky steps and changed files (agent_tests.py): verdicts in alerts and answers
    "agent_fix_loop": False,    # with Mint's Claude Code hooks: send Claude back when it stops or pushes with failing / untested tests
    # The guard (guard.py): ask before deleting / changing / risky commands. all / delete / off.
    "guard": "all",
    # Google Meet calls with Mint (meet_call.py): share the whole screen ("screen") or not ("off"); the longest call.
    "meet_share": "screen",
    "meet_max_minutes": 120,
    # Who is let into Mint's call without asking: everyone (whoever has the link) / first / ask (Telegram).
    "meet_admit": "everyone",
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
                  "dictate_toggle": "", "clipboard": "ctrl+option+v",
                  "hide": "ctrl+option+h"},
    # Words dictation should spell exactly (names, jargon), comma-separated.
    "dictation_words": "",
    # Where screenshots go: "both" (a file and the clipboard), "clipboard" or "file".
    "screenshot_to": "both",
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
    # After Mint is done, it listens this many seconds for a follow-up, then needs the wake word
    # again (listening.py). 0 = the wake word every time.
    "follow_up_seconds": 6,
    # While Mint only waits for "Hey Mint", the plain microphone: other apps' sound (a video, music) is not turned
    # down and the mic hears as it is. Echo cancellation comes on in a conversation (audio_vp). False = always on.
    "quiet_while_waiting": True,
    # A voice model that stalls, answers slowly or fails: use the next one for a while (live_models.py).
    "switch_when_slow": True,
    # The voice models to use, best first ([] = Mint's pool), and each one's tokens-a-minute limit.
    "voice_models": [],
    "live_tpm_limit": 65000,
    # Extra words and names for Mint to expect (see vocab.py).
    "vocabulary": [],
    # The welcome window (onboarding.py) has been seen, finished or skipped.
    "onboarded": False,
    # Who's who.
    "assistant_name": "Mint",
    "user_name": "",
    "about_me": "",
    # How the assistant comes across, in the user's words (Settings > General > Personality).
    # Up to 100 characters; "" = its default personality.
    "personality": "",
    # How Mint sounds: a Gemini live voice ("" = config.VOICE) and a free-text style.
    "voice_name": "",
    "speaking_style": "",
    # The language Mint answers in: "auto" follows the user; otherwise always this one.
    "reply_language": "auto",
    # The wake phrase ("" = "Hey <assistant name>"); wake_models: more phrases that also wake it.
    "wake_phrase": "",
    "wake_models": [],
    # Shortcuts (the Shortcuts app) Mint may not run, by name; empty = all allowed.
    "shortcuts_blocked": [],
    # The mini music player shows by itself when music starts (Spotify / Music).
    "music_player_auto": True,
    # Updates of the packaged app: checked at launch and every 12 h, installed while the Mac is idle.
    "auto_update": True,
    "update_channel": "stable",
    # Mishearings the user corrected (hearing.py; the fix_hearing tool).
    "hearing_fixes": [],
    # Near the Live context limit, compact the conversation at a quiet moment instead of letting the
    # server drop its oldest turns (compaction.py).
    "auto_compact": True,
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
    # Telegram remote control (telegram.py): on only with a bot token and this switch; read-only
    # phone requests never send, delete or buy; notify sends tracker and agent news to the chat.
    "telegram_enabled": False,
    "telegram_read_only": False,
    "telegram_notify": True,
    # Email remote control (email_remote.py): requests emailed to `email_address` from the allowed senders
    # ("list": email_allowed, empty = the address itself; "all" needs a secret word), with subjects starting
    # with email_prefix; replies only to the verified sender. Read through IMAP + SMTP ("imap": an app password
    # in the Keychain; IDLE, seconds) or Apple Mail ("mail": the account added in Internet Accounts, no password;
    # email_mail_account "" = every inbox). "auto": IMAP when the address has an app password, else Mail.
    "email_enabled": False,
    "email_address": "",
    "email_allow": "list",
    "email_allowed": "",
    "email_prefix": "Mint:",
    "email_read_only": False,
    "email_imap_host": "",             # "" = from the address (Gmail, iCloud, Fastmail, Yahoo; else imap.<domain>)
    "email_smtp_host": "",             # "host" or "host:port"; "" = from the address
    "email_auth_server": "",           # whose Authentication-Results to trust; "" = mx.google.com for Gmail
    "email_backend": "auto",           # "auto" (app password saved: IMAP, else Mail) | "imap" | "mail"
    "email_mail_account": "",          # the Mail account watched; "" = every inbox
    "stop_words": ["stop", "stop it", "stop everything", "cancel", "cancel that", "abort",
                   "enough", "hold on", "never mind", "nevermind", "shut up"],
    # The voice session declares only the everyday tools; the rest are found with find_tools and run with
    # use_tool (tool_diet.py). False = declare every tool, as before (takes effect at Mint's next start).
    "tool_diet": True,
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


_queue = None                  # the worker that tells listeners about main-thread changes (_notify)


def _tell(changes: list) -> None:
    for key, value in changes:
        for listener in list(_listeners):
            try:
                listener(key, value)
            except Exception:
                log.exception("settings listener failed")


def _notify(changes: list) -> None:
    """Run the listeners. A change made on the main thread (a Settings switch, a menu click, a hand
    edit picked up by the menu's poll) is passed on to one worker thread, in order: listeners do real
    work - reload the wake word models, rebuild the audio engine, reconnect - and on the main thread
    that froze every window until it finished, or for good if it waited on a busy thread. Listeners
    already run on whichever thread changed the setting; the value itself is saved before this."""
    if not changes or not _listeners:
        return
    if threading.current_thread() is not threading.main_thread():
        _tell(changes)
        return
    global _queue
    with _lock:
        if _queue is None:
            import queue
            _queue = queue.Queue()

            def work():
                while True:
                    _tell(_queue.get())
            threading.Thread(target=work, daemon=True, name="mint-settings-changed").start()
    _queue.put(list(changes))


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
