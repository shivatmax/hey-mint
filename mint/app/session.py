"""The Gemini Live session: audio in, audio out, tool calls, wake and sleep.

Microphone routing is the heart of it:

    asleep  ->  mic frames go ONLY to the local wake word model. Nothing is sent.
    awake   ->  mic frames stream to Gemini Live.
    paused  ->  mic frames are dropped entirely, wake word included.
"""

from __future__ import annotations

import asyncio
import math
import re
import base64

import numpy as np
import threading
import datetime as dt
import logging
import json
import os
import sys
import time
import traceback

from google import genai
from google.genai import types

from mint.core import config
from mint.voice import listening
from mint.tools import everyday as skills
from mint.tools import registry as tools
from mint.voice.audio import Audio
from mint.voice.wake import phrase_cut as wake_phrase_cut

log = logging.getLogger("mint.app.session")


_live_env = ""          # which Gemini key the Live session is using (gemini_keys.py)


def _client() -> genai.Client:
    """The Live client, on the key gemini_keys picks for voice (key 1, or key 2 while key 1 is set aside)."""
    global _live_env
    from mint.core import gemini_keys
    try:
        _live_env, key = gemini_keys.live_key()
    except KeyError:
        _live_env, key = config.API_KEY_ENV, os.environ[config.API_KEY_ENV]
    return genai.Client(http_options={"api_version": "v1beta"}, api_key=key)


# Set by Mint.compact(): the compacted conversation the next session starts from.
_carry_over = ""
_STARTED = time.time()           # this run's first session: compaction reads the history from here on
ANSWER_NUDGE = 1.2      # the user's voice stopped and nothing came back: close their audio for the voice service
ANSWER_RESEND = 5.0     # and still nothing: send their words (said right after "Hey Mint") as text


def _permissions_note() -> str:
    """Which permissions Mint has, so it knows what it can't do before it tries (permit.py)."""
    try:
        from mint.core import permit
        return permit.prompt_note()
    except Exception:
        log.debug("permissions note", exc_info=True)
        return ""


_SCREEN_TOOLS = {"ui_act", "ui_elements", "click_text", "click_at", "type_text", "press_key", "scroll", "drag",
                 "read_window", "look", "switch_to", "open_app", "menu"}
_MISSED = ("FAILED", "NOT CLICKED", "NOT DRAGGED", "SUSPECTED NO-OP", "No text is visible", "UNVERIFIED",
           "REFUSED", "NOT RUN")

_ASKS_PROGRESS = re.compile(r"\b(what\W*(s|is|are)?\W+(it'?s|its|it|that|the job|the task|you)\W+(are\W+|is\W+)?doing|how (much|far)|progress|status|"
                            r"is it (working|done|running)|still (working|going)|any update|what happened|"
                            r"i don'?t see (it|anything))\b", re.I)
CLICK_AT_MISSES = 3          # rough-point clicks that missed in one request before click_at is turned off for it


def _ladder(owner, name: str, result: str) -> str:
    """After two screen actions in one request didn't work: the next way to try, so the model changes method
    instead of retrying variants of the same call (seen 9 Oct: ui_act, ui_act, type_text, click_text x2 in
    Telegram, then "try it yourself"). '' while things work."""
    if name not in _SCREEN_TOOLS:
        return ""
    track = owner.__dict__.setdefault("_misses", {"asked": 0.0, "n": 0})
    if track["asked"] != getattr(owner, "_asked_at", 0.0):
        track.update(asked=getattr(owner, "_asked_at", 0.0), n=0)
    if not result.startswith(_MISSED):
        return ""
    track["n"] += 1
    if track["n"] < 2:
        return ""
    return (f"\n[{track['n']} tries in this request didn't work. Change method, don't repeat: 1) is the app's window "
            "really showing? look - if not, open_app it again; 2) read what is there: ui_elements, or read_window "
            "if it lists nothing; 3) click the words you see with click_text (a search box by its placeholder, an "
            "item by its name); 4) a keyboard shortcut or the menu (menu action=list); 5) look + click_at naming "
            "the target. After it opens something, read_window and check it is the right one before step_done.]")


def control_stopped() -> bool:
    try:
        from mint.app import control
        return bool(control.stopped())
    except Exception:
        return False


def _carry_on_note(mint) -> str:
    """For a new voice session started in the middle of a job: what the job is, where it stands, and what the user
    said - so it carries on by itself."""
    parts = ["(Mint - not the user: the voice model was switched mid-job to stay under the per-minute limit. Carry on "
             "now from where it stopped, without asking.)"]
    try:
        from mint.app import autopilot
        from mint.app import tasks
        task = getattr(mint, "task", None)
        if task:
            parts.append("The plan:\n" + tasks.outline(task))
        request = autopilot._state.get("request") or ""
        if request:
            parts.append(f"The user asked: \u201c{request[:300]}\u201d")
    except Exception:
        pass
    said = [text for _, text in getattr(mint, "_user_lines", [])][-3:]
    if said:
        parts.append("The user's latest words (they override the plan): " + " | ".join(f"\u201c{t}\u201d" for t in said))
    return "\n".join(parts)


def _by_modality(meta) -> str:
    """' (text 9k, audio 12k, image 300k)': what the context is made of, to see what fills it."""
    try:
        parts = []
        for detail in getattr(meta, "prompt_tokens_details", None) or []:
            kind = str(getattr(detail, "modality", "") or "").split(".")[-1].lower()
            count = int(getattr(detail, "token_count", 0) or 0)
            if count:
                parts.append(f"{kind} {count // 1000}k")
        return f" ({', '.join(parts)})" if parts else ""
    except Exception:
        return ""


def _live_config() -> types.LiveConnectConfig:
    # The model has no clock. Giving it the start time lets it reason about
    # "tomorrow at nine"; get_status covers long sessions.
    local = dt.datetime.now().astimezone()
    now = local.strftime("%A %B %-d %Y, %-I:%M %p")
    offset = local.strftime("%z")
    zone = f"{local.tzname()} (UTC{offset[:3]}:{offset[3:]})"
    from mint.app import compaction
    from mint.core import custom
    from mint.knowledge.conversation import memory
    from mint.tools.diet import clip
    personal = custom.prompt_addendum()
    # The running notes grow with use: capped, so the prompt re-read on every tool step stays small (9 Oct).
    remembered = clip(memory.summary(), 1200, "recall_history has more")
    from mint.voice import vocab
    words = vocab.prompt_text()
    from mint.core import prefs as _prefs
    name = _prefs.name()
    base = config.SYSTEM_INSTRUCTION if name == "Mint" else config.SYSTEM_INSTRUCTION.replace("Mint", name)
    # "It is 8:45 AM UTC" (27 Sep, 14:15 in India): the model knows the moment
    # but not the user's zone. Name it, and always answer in it.
    instruction = (f"{base}\n\nSession started: {now}, the user's local time, {zone}. Always give "
                   f"times and dates in {zone}, never UTC; for the time now, call get_status."
                   + ("" if config.desktop_engine() else
                      "\n\nThe desktop tool is not installed on this Mac: wherever these "
                      "instructions mention it, use ui_act (or click_text) instead.")
                   + f"\n\n{_permissions_note()}"
                   + (f"\n\n{personal}" if personal else "")
                   + (f"\n\n{words}" if words else "")
                   + (f"\n\nWhat you remember from earlier conversations with this user "
                      f"(your own notes; use them, do not recite them):\n{remembered}" if remembered else "")
                   + (f"\n\n{jobs}" if (jobs := _jobs_note()) else "")
                   + (f"\n\n{compaction.framed(clip(_carry_over, 2000))}" if _carry_over else ""))
    from mint.tools import diet as tool_diet
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        media_resolution="MEDIA_RESOLUTION_MEDIUM",
        system_instruction=types.Content(parts=[types.Part(text=instruction)], role="user"),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.VOICE)
            )
        ),
        # Long sessions would otherwise hit the context limit and drop.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=104857,
            sliding_window=types.SlidingWindow(target_tokens=52428),
        ),
        # Lets the session be resumed after a network drop instead of losing history.
        session_resumption=types.SessionResumptionConfig(handle=None),
        # Captions of both sides, for the HUD and the log. The audio is still
        # the conversation; this is only a transcript of it.
        # The user's own words bias the transcript that the captions, the
        # "stop" check and the talking-to-me check read.
        # Indian English. Adding "hi-IN" here was tried and backfired: English
        # speech came back transliterated into Devanagari ("हे मिंट, व्हाट टाइम
        # इज इट?") and the model then answered in Hindi. Hindi and Hinglish are
        # still understood - the model hears the audio itself; see the prompt.
        input_audio_transcription=types.AudioTranscriptionConfig(
            custom_vocabulary=vocab.terms() or None, language_codes=["en-IN"]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Low start sensitivity: in testing, room noise and a nearby
        # conversation were taken as requests ("you can.", "Is this Mary Jane's
        # house?"). Deliberate speech still triggers easily.
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=200,
                silence_duration_ms=700,
            )
        ),
        # The everyday tools plus find_tools/use_tool for the rest (tool_diet.py; pref tool_diet false = all).
        tools=_named(tool_diet.live_tools(tools.tools()), name),
    )


def _named(declared: list, name: str) -> list:
    """Tool descriptions speak of the assistant by name: use the current one."""
    if name == "Mint":
        return declared
    for tool in declared:
        for function in getattr(tool, "function_declarations", None) or []:
            if function.description:
                function.description = function.description.replace("Mint", name)
    return declared


# Tools that bring a new window or tab to the front.
_OPENERS = {"open_app", "open_url", "open_folder", "open_chrome", "open_slack"}



# Apps where Return after typing sends a message: there, the same words twice within two minutes are a duplicate
# message. Elsewhere it's a search or a command typed again (VS Code's extension search was blocked 4 times).
_MESSAGING = ("slack", "messages", "whatsapp", "telegram", "discord", "mail", "teams", "zoom", "signal",
              "claude", "chatgpt", "codex", "messenger", "wechat", "line", "skype", "outlook", "spark",
              "chrome", "safari", "arc", "brave", "firefox", "edge", "opera", "vivaldi", "comet")


def _is_messaging(name: str, bundle: str) -> bool:
    import re as _re
    words = set(_re.findall(r"[a-z]+", f"{name} {bundle}".lower()))
    return bool(words & set(_MESSAGING)) or "mobilesms" in words


def _messaging_front() -> bool:
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return _is_messaging(app.localizedName() or "", app.bundleIdentifier() or "") if app else True
    except Exception:
        return True

TRACE = os.path.expanduser("~/Library/Logs/Mint/tools.log")
TRACE_MAX = 4_000_000        # bytes; the older half is dropped past this


def _trace(name: str, args: dict, result: str) -> None:
    """Every tool call with its arguments and its whole result, in ~/Library/Logs/Mint/tools.log: mint.log keeps
    one short line per call, which hid what Mint actually saw (a VS Code run looked like 4 controls)."""
    try:
        if os.path.exists(TRACE) and os.path.getsize(TRACE) > TRACE_MAX:
            with open(TRACE, "rb") as fh:
                fh.seek(TRACE_MAX // 2)
                keep = fh.read()
            with open(TRACE, "wb") as fh:
                fh.write(keep[keep.find(b"\n====") + 1:])
        shown = json.dumps(args, ensure_ascii=False, default=str)[:600]
        with open(TRACE, "a", encoding="utf-8") as fh:
            fh.write(f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} {name} {shown}\n{str(result)[:8000]}\n")
    except Exception:
        log.debug("tool trace failed", exc_info=True)

class _NoUI:
    """Stand-in when there is no menu bar or HUD."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Mint:
    meet = None                     # the Google Meet call going on (meet_call.Call), set by meet_begin
    live = None                     # the running session (set in run): gc scans from threads crashed Mint (6 Oct)

    def __init__(self, text_mode: bool = False, hands_free: bool = False,
                 wake_word: str = "hey_mint", wake_threshold: float = 0.5,
                 sleep_after: float = 12.0, half_duplex: bool | None = None, ui=None) -> None:
        # Voice processing (echo cancellation) when available: the mic can then
        # stay open while Mint speaks, so the user can interrupt. Otherwise
        # fall back to PyAudio and close the mic while speaking (half duplex).
        from mint.core import prefs as _prefs
        self._forced_half_duplex = bool(half_duplex)
        try:
            from mint.app import ear as _ear
            from mint.voice.engine import VoiceAudio
            self.audio = VoiceAudio(echo=str(_prefs.get("echo_cancellation") or "auto"),
                                    input_uid=str(_prefs.get("input_device") or ""),
                                    output_uid=str(_prefs.get("output_device") or ""),
                                    defer=_ear.defer_audio(),
                                    quiet_asleep=self._quiet_asleep_wanted())
            echo_cancelled = self.audio.full_duplex
            self.audio.on_reconfigure = self._audio_reconfigured
            self._voice_allowance(self.audio)
            self._print(f"[audio: {self.audio.status}]")
        except Exception as error:
            log.warning("audio engine unavailable (%s); using half duplex", error)
            self.audio = Audio()
            echo_cancelled = False
        if half_duplex is None:
            half_duplex = not echo_cancelled
        self._mic_lent_to = ""                 # a call or meeting app has the mic (Mint stepped aside entirely)
        self._in_call = ""                     # a call app has the mic and Mint still listens for the wake word
        self._call_silence = 0.0               # seconds of digital silence from the mic during a call
        self._call_silence_told = False
        self._wake_refused_at = 0.0            # the voice check last turned a wake word away (see _wake_is_user)
        self._mic_probes: list = []            # callbacks fed every microphone frame (the mic test, mictest.py)
        self._mic_testing = False              # a mic test is running: Mint hears but does not act
        self._barge_frames = 0
        self._noise_floor = 0.005
        # Spoken replies. Off = silent: replies appear in the HUD, not the speaker.
        from mint.core import prefs  # noqa: F811
        self.voice_on = bool(prefs.get("voice"))
        self._silent_reply = False
        self.text_mode = text_mode
        self.ui = ui or _NoUI()
        self.session = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.audio_in: asyncio.Queue = asyncio.Queue()
        self.out_queue: asyncio.Queue = asyncio.Queue(maxsize=40)
        self._resume_handle: str | None = None
        # A Google Meet call (meet_call.Call) while one is on: the call is the microphone and the speaker.
        self.meet = None
        self._meet_q: asyncio.Queue = asyncio.Queue(maxsize=200)

        # --- hands-free state ----------------------------------------------
        self.hands_free = hands_free
        self.sleep_after = sleep_after
        self.half_duplex = half_duplex
        self.asleep = hands_free
        self.paused = False
        self._wake = None
        self._preroll = None
        self._wake_word = wake_word
        self._last_voice = 0.0
        self._busy = False  # a tool is running; do not fall asleep mid-action
        self._heard_during_work = False
        # Keep hearing the user while tools run (they asked for it); "stop"
        # cuts everything off. prefs "listen_while_working" false restores the
        # old wake-word-only behaviour during work.
        self.listen_while_working = bool(prefs.get("listen_while_working"))
        self._tool_task: asyncio.Task | None = None
        # One tool batch at a time. With tools in the background, Gemini's next
        # batch could start while the last was still running: in testing that
        # gave plan_task x4 and open_app x4 with results out of order.
        self._tool_lock = asyncio.Lock()
        self._stop_epoch = 0             # bumped by "stop": queued batches must not run after it
        self._halted = False             # "stop" said: tool calls still coming in that turn are refused
        self._restarting = False         # new_session() is closing the session on purpose
        self._recent_calls: dict = {}    # (name, args) -> (time, result), to catch repeats
        self._batch: list = []           # [(id, name)] of the running tool batch
        self._batch_results: dict = {}   # id -> result, for the ones finished
        self._stop_armed = True          # one stop per utterance
        self._last_stop = 0.0

        # --- captions ---------------------------------------------------------
        self._heard = ""       # what the user said this turn
        self._said = ""        # what Mint said this turn
        self._turn_open = False

        self._timers: set[asyncio.Task] = set()
        # The current long task, if any: {goal, steps, done: {step: result}}.
        self.task: dict | None = None

        # --- voice lock and who-was-that-for ---------------------------------
        self._gate = None               # voicelock.Gate while the lock is on
        self.enroller = None            # enroll.Enroller while training the voice
        # Was it meant for Mint? (listening.py) Every voice turn gets a verdict - act or
        # ignore - before Mint's reply is played or a tool runs; a turn the wake word
        # opened is trusted more than a follow-up.
        self._next_kind = "follow"      # the next voice turn: asked / wake / follow (listening.quick)
        self._kind = "follow"
        self._voice_turn = False        # this turn has the user's speech in it
        self._verdict: str | None = None    # "act" / "ignore" for that speech; None: not decided yet
        self._verdict_ready: asyncio.Event | None = None
        self._check_task: asyncio.Task | None = None
        self._held: list[bytes] = []    # Mint's reply audio, held until the verdict
        self._held_words: list[str] = []
        self._seg = 0                   # where in _heard the speech being judged starts
        self._mid_turn = False          # that speech came in a turn already under way
        self._mute = False              # Mint's spoken reaction to speech that was not for it: dropped
        self._dropping = False          # the rest of an ignored utterance: not kept
        self._guard_said = False        # the words answered the guard's question
        self._last_chunk_at = 0.0
        self._turn_began = 0.0
        self._suppress_turn = False     # the user was talking to someone else
        self._last_said = ""
        self._turn_levels: list[float] = []
        self._ignored_at = 0.0
        self._pending_text: list[str] = []   # typed while reconnecting; sent once connected
        self._server_at = 0.0                 # the last real message from the server (the answer watchdog)
        self._spoke_at = 0.0                  # when the user's last words sent to Gemini ended (the voice watch)
        self._voice_watch_task = None
        self._deaf_voice = 0
        self._unanswered = ""                # the last typed request, until the server answers it
        self._wake_cut = 0                  # bytes of an unsure wake's held audio that are the phrase
        self._woke_at = 0.0                 # when the wake word last woke Mint
        # Mint Ear (mint/app/ear.py): the hand-over from the launcher, and unloading when idle.
        self.ear = None                     # ear.Link while the launcher hands audio over
        self._ear_feeding = False           # the microphone is the Ear's until ours catches up
        self._mic_alive = False
        self._own_mic = asyncio.Event()     # set: our microphone may start (see _ear_feed)
        self._last_active = time.monotonic()
        self._ever_awake = False
        self._pending_wake = 0.0        # when an unsure "Hey Mint" is being checked
        # How long Mint keeps listening without the wake word (listening.Window): a few
        # seconds after it is done, not a quiet clock that any sound restarts.
        from mint.voice import listening
        self._window = listening.Window(listening.follow_up_seconds(), min(sleep_after, listening.WAKE_GRACE))
        self._tools_at = 0.0                # when the last tool batch ended
        self._goodbye_done = False
        # Not every sound needs an answer ("hmm", "okay"), and a goodbye gets one
        # short line, not three (see _filler_turn and _hush).
        self._filler_checked = False
        self._filler_hit = False
        self._typed_turn = False
        self._hush = False                  # after a goodbye: nothing more is said until woken
        self._farewell_bytes = -1           # >= 0 while the goodbye line plays: bytes let through

        if hands_free:
            from mint.voice.wake import MintWake, PreRoll, WakeWord
            # "Hey Mint" is trained here (wake.MintWake); the others are openWakeWord's.
            self._wake = (MintWake() if wake_word == "hey_mint"
                          else WakeWord(wake_word, wake_threshold))
            # 1.5 s: the whole of "Hey Mint" plus a little, for the voice check.
            self._preroll = PreRoll(seconds=2.0, rate=config.SEND_SAMPLE_RATE)
        self.refresh_voice_lock()

    def refresh_voice_lock(self) -> None:
        """Turn the voice lock on or off to match settings and enrolment."""
        from mint.core import prefs
        from mint.voice import voicelock
        lock = voicelock.lock
        on = bool(prefs.get("voice_lock")) and lock.enrolled
        outdated = lock.stale or (lock.enrolled and lock.templates is None)
        if outdated and voicelock.RECORDINGS.exists() and not getattr(self, "_recalibrating", False):
            # A voiceprint from an older version (other features, or before the
            # wake-phrase check): rebuild it - and the personal "Hey Mint" -
            # from the saved enrolment audio. No retraining needed. (The user's
            # voiceprint went stale on 24 Sep and the lock silently stayed off
            # for two days; their recordings were on disk the whole time.)
            self._recalibrating = True

            def recalibrate():
                try:
                    recorded = voicelock.load_recordings()
                    if not recorded or not recorded[0]:
                        return
                    self._print("[voice lock: rebuilding from your saved recordings (about a minute)…]")
                    report = lock.enroll(*recorded)
                    from mint.voice.enroll import train_personal_wake
                    report["hey_mint"] = train_personal_wake(recorded[1], recorded[0])
                    self._print(f"[voice lock: rebuilt from your saved recordings: consistency "
                                f"{report['consistency']}, Hey Mint {report['hey_mint']}]")
                    skills.notify(prefs.name(), "Your voice lock is on again - rebuilt from your "
                                                "saved voice recordings, no retraining needed.")
                    if self.loop is not None:
                        self.loop.call_soon_threadsafe(self.refresh_voice_lock)
                    else:
                        self.refresh_voice_lock()
                except Exception as error:
                    log.exception("rebuilding the voiceprint failed")
                    self._print(f"[voice lock: could not rebuild from the recordings ({error}) - "
                                "please retrain your voice]")
                finally:
                    self._recalibrating = False
            threading.Thread(target=recalibrate, daemon=True, name="voiceprint-rebuild").start()
        # The voiceprint was recorded through echo cancellation; the plain microphone
        # (used while waiting, so other apps' sound is not ducked) hears a voice so
        # differently that the user's own "Hey Mint" scored 0.0-0.25 against a 0.5
        # bar (7-8 Oct log) and was ignored as "another voice". With the lock on,
        # Mint listens through the microphone it was trained on.
        set_mode = getattr(self.audio, "set_mode", None)
        if set_mode is not None:
            set_mode(quiet_asleep=self._quiet_asleep_wanted(on),
                     reason="voice lock: the microphone it was trained on" if on else "voice lock off")
        if on and self._gate is None:
            self._gate = voicelock.Gate(lock)
            # Load the model now, off the audio path (~0.1 s, +90 MB).
            threading.Thread(target=lambda: lock.score(np.zeros(8000, np.float32) + 1e-4),
                             daemon=True).start()
        elif not on:
            self._gate = None
        if self._wake is not None and hasattr(self._wake, "load"):
            self._wake.load()        # picks up a freshly trained personal "Hey Mint"
        self._print(f"[voice lock {'on' if on else 'off'}"
                    + ((" - rebuilding your voiceprint from your saved recordings" if getattr(self, "_recalibrating", False)
                        else " - the voice model was upgraded: please retrain your voice") if lock.stale
                       else "" if lock.enrolled else " - not trained yet")
                    + (" - retrain to check 'Hey Mint' against your own recordings"
                       if lock.enrolled and lock.templates is None else "") + "]")

    # --- state ---------------------------------------------------------------

    def _state(self, name: str, note: str = "") -> None:
        try:
            self.ui.set_state(name, note)
        except Exception:
            log.debug("ui update failed", exc_info=True)
        from mint.app import telegram
        telegram.on_event("state", {"name": name, "note": note})     # for /status

    def _idle_state(self) -> str:
        if self.paused:
            return "paused"
        return "sleeping" if self.asleep else "awake"

    async def wake_up(self, reason: str = "wake word") -> None:
        if self.paused or not self.asleep:
            return
        self.asleep = False
        self._last_voice = self._last_active = time.monotonic()
        self._ever_awake = True
        self._hush = False
        self._farewell_bytes = -1
        self._woke_at = time.monotonic() if reason.startswith("wake word") else 0.0
        # The first thing said after the wake word is for Mint unless it is a stray scrap; after
        # Mint woke itself (an agent's news) it is a follow-up; a shortcut or the menu is on purpose.
        self._next_kind = "wake" if reason.startswith("wake word") else "follow" if reason == "agent" else "asked"
        from mint.voice import listening
        self._window.follow_up = listening.follow_up_seconds()
        self._window.woke(time.monotonic())
        self._suppress_turn = False
        if reason != "shortcut":
            # Opening the console keeps the last exchange on screen to read.
            self.ui.new_exchange()
        if self._preroll is not None and reason == "wake word":
            # Replay the moment just before waking, so the words spoken right
            # after the wake phrase are not lost - minus the phrase itself.
            tail = self._after_wake_phrase(self._preroll.drain())
            if tail and self.session is not None:
                await self.out_queue.put(tail)
            if self._gate is not None:
                # The wake phrase was voice-checked: the rest of this breath is
                # the user too, so it streams without being held again.
                self._gate.force_open()
        log.info("awake (%s)", reason)
        self._print(f"[awake: {reason}]")
        self._state("awake")

    async def hold_to_talk(self, down: bool) -> None:
        """The hold-to-talk keys (fn+⌃ by default, ui._register_talk_hold). Held: the words are recorded
        (dictation.talk, from the instant the keys went down - Mint's own stream, or a plain microphone when its
        engine is off). Let go: Mint wakes and the clip goes to the model as the user's turn, with its end marked.
        Seen 10 Oct 19:22: waking first and streaming live put Mint to sleep in the same second (the follow-up clock
        saw no server activity for minutes) and, paused, the restarting audio engine heard nothing."""
        from mint.voice import dictation
        if down:
            dictation.talk.begin()              # (already on since the keys went down: keeps what it has)
            self._ptt = True
            if self.audio.playing or not self.audio_in.empty():
                self._flush_playback()          # talking over Mint: the user has the floor
            self._print("[listening while the keys are held]")
            self._state("awake")                # the orb shows it is listening
            return
        if not getattr(self, "_ptt", False):
            return
        self._ptt = False
        pcm = dictation.talk.end()
        seconds = len(pcm) / 2 / dictation.RATE
        if not dictation._spoken(pcm):
            self._print(f"[hold to talk: nothing heard in {seconds:.1f}s]")
            self._state(self._idle_state())
            return
        self._print(f"[keys let go: {seconds:.1f}s of words - working on it]")
        if self.paused:
            self._ptt_repause = True
            self.set_paused(False)
        if self.asleep:
            await self.wake_up("shortcut hold")
        else:
            self._next_kind = "asked"
        self._window.woke(time.monotonic())      # time for the words' verdict; a request keeps it open
        self._server_at = time.monotonic()       # (the stale-request clock starts now, not at the last reply)
        session = self.session
        if session is None:
            return
        step = 3200                              # 0.1 s chunks
        try:
            for at in range(0, len(pcm), step):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[at:at + step], mime_type="audio/pcm;rate=16000"))
        except Exception:
            log.debug("hold-to-talk audio not sent", exc_info=True)
        await self._close_user_audio()           # the words are over now - no waiting for a pause

    def _converse(self, on: bool) -> None:
        """Echo cancellation (voice processing) only while talking with the user: asleep it ducked every other
        app's sound - a video, music - and changed what the mic hears (7 Oct). audio_vp.VoiceAudio.converse."""
        converse = getattr(getattr(self, "audio", None), "converse", None)
        if converse is not None and (on or getattr(self, "hands_free", True)):
            try:
                converse(on)
            except Exception:
                log.debug("switching the audio mode failed", exc_info=True)

    def go_to_sleep(self, reason: str = "quiet") -> None:
        if self.asleep or not self.hands_free or self.meet is not None:
            return
        if getattr(self, "_ptt_repause", False):
            self._ptt_repause = False           # woken by the hold-to-talk keys while paused: paused again
            self.set_paused(True)
            return
        self.asleep = True
        self._last_active = time.monotonic()
        self._next_kind = "follow"          # words that still arrive were not opened by a wake
        if self._wake is not None:
            self._wake.reset()
        if self._gate is not None:
            self._gate.cancel()
        # The mic stops streaming now; tell the server, or its voice detection
        # can sit waiting for the end of "speech" and hold the next request.
        if self.loop is not None and self.session is not None:
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._end_audio_stream()))
        self._converse(False)                # back to the plain mic: nothing else ducked while Mint waits
        log.info("asleep (%s)", reason)
        self._print(f"[asleep: {reason}]")
        self._state(self._idle_state())

    def set_paused(self, paused: bool) -> None:
        """Microphone fully off (wake word included) while paused."""
        self.paused = paused
        if paused:
            self.asleep = self.hands_free
            self._audio_rest_later()
        elif getattr(self.audio, "resting", False):
            threading.Thread(target=self.audio.wake, name="audio-wake", daemon=True).start()
        self._print("[paused]" if paused else "[resumed]")
        self.ui.set_paused(paused)
        if paused and self.loop is not None and self.session is not None:
            # The mic may have gone off mid-sentence: tell the server the speech
            # is over, or its next typed turn waits for it (see _close_user_audio).
            asyncio.run_coroutine_threadsafe(self._close_user_audio(), self.loop)

    def _audio_rest_later(self, after: float = 3.0) -> None:
        """Paused with nothing to say: let the audio engine stop (audio_vp.rest) until the mic is on again or Mint
        speaks. Checked again a moment later, so a reply still playing finishes first."""
        rest = getattr(self.audio, "rest", None)
        if rest is None:
            return

        def check() -> None:
            if self.paused and self.meet is None and not self.audio.playing and self.audio_in.empty():
                rest()
        timer = threading.Timer(after, check)
        timer.daemon = True
        timer.start()

    @staticmethod
    def _voice_allowance(audio) -> None:
        """The voice lock's allowance for the plain mic (voicelock.PLAIN_MIC): asleep on speakers Mint no longer
        uses voice processing, through which the voiceprint was recorded."""
        from mint.voice import voicelock
        voicelock.capture_shift = voicelock.PLAIN_MIC if getattr(audio, "plain_on_speakers", False) else 0.0

    def _audio_reconfigured(self, audio) -> None:
        """The engine was rebuilt (devices, echo mode, or it had stopped)."""
        self._voice_allowance(audio)
        if not audio.playing:
            audio.muted = False                # a fresh engine, nothing playing: the mic is open
        if not self._forced_half_duplex:
            # Without echo cancellation on speakers, the mic must close while
            # Mint talks or it hears itself; with headphones it need not.
            self.half_duplex = not audio.full_duplex
        self._print(f"[audio: {audio.status}{'' if self.half_duplex is False else ' - mic closes while speaking'}]")

    def apply_audio_settings(self) -> None:
        """Any thread: the user changed the microphone, speaker or echo setting."""
        from mint.core import prefs
        configure = getattr(self.audio, "configure", None)
        if configure is None:
            return
        threading.Thread(target=configure, kwargs=dict(
            echo=str(prefs.get("echo_cancellation") or "auto"),
            input_uid=str(prefs.get("input_device") or ""),
            output_uid=str(prefs.get("output_device") or "")), daemon=True).start()

    async def _mic_share_watch(self) -> None:
        """A call or meeting app has the microphone.

        Two apps capturing at once - and Mint's echo canceller next to a call's
        own - is what made calls and meetings misbehave. Every 1.5 s this asks
        CoreAudio which apps are capturing (listeners on that do not fire on
        current macOS). When a call app is: by default (listen_in_calls) Mint
        drops its echo canceller and keeps listening for the wake word on the
        plain microphone - before 8 Oct it let go of the mic entirely, so "Hey
        Mint" in a Google Meet was never heard. With listen_in_calls off it
        steps aside completely, as before. Either way it takes the usual
        microphone back 3 s after the call app stops capturing."""
        from mint.voice import devices as audio_devices
        from mint.core import prefs
        if not hasattr(self.audio, "suspend"):
            return
        free_since = 0.0
        while True:
            await asyncio.sleep(1.5)
            if self.meet is not None:
                continue                          # Mint's own Google Meet call: its audio is Mint's already
            if not prefs.get("share_mic"):
                if self._mic_lent_to or self._in_call:
                    self._call_over()
                continue
            try:
                users = await asyncio.to_thread(audio_devices.mic_users)
            except Exception:
                continue
            callers = [u for u in users if _is_call_app(u["bundle"], prefs.get("share_mic_apps") or [])]
            if callers and not (self._mic_lent_to or self._in_call):
                name = _app_label(callers[0])
                self._flush_playback()
                if self._gate is not None:
                    self._gate.cancel()
                if prefs.get("listen_in_calls") is not False and hasattr(self.audio, "set_mode"):
                    self._in_call = name
                    self._call_silence, self._call_silence_told = 0.0, False
                    if not self.asleep and not self._busy:
                        self.go_to_sleep(f"a {name} call started")
                    self.audio.set_mode(in_call=True, reason=f"{name} is in a call: the plain microphone")
                    self._print(f"[{name} is using the microphone (a call?) - Mint keeps listening for "
                                f"“Hey {prefs.name()}” only, without echo cancellation]")
                    self._state(self._idle_state(), f"In a call - say “Hey {prefs.name()}”")
                else:
                    self._mic_lent_to = name
                    await asyncio.to_thread(self.audio.suspend, name)
                    self._print(f"[{name} is using the microphone - Mint steps aside until it's done]")
                    self._state("paused", f"{name} is using the mic")
                self._offer_meeting_notes(name)
                free_since = 0.0
            elif callers:
                free_since = 0.0
            elif self._mic_lent_to or self._in_call:
                free_since = free_since or time.monotonic()
                if time.monotonic() - free_since >= 3.0:
                    self._call_over()
                    free_since = 0.0

    def _call_over(self) -> None:
        """The call app let go of the microphone (or sharing was switched off): the usual microphone again."""
        if self._in_call:
            name, self._in_call = self._in_call, ""
            try:
                from mint.tools import meetings
                meetings.call_ended()
            except Exception:
                pass
            set_mode = getattr(self.audio, "set_mode", None)
            if set_mode is not None:
                set_mode(in_call=False, reason="the call is over")
            self._print(f"[{name} is done with the microphone - Mint is back to its usual microphone]")
            self._state(self._idle_state())
        if self._mic_lent_to:
            self._take_mic_back()

    def _call_mic_check(self, pcm: bytes) -> None:
        """During a call: a call app that takes the microphone for itself leaves everyone else digital
        silence. Say so once, rather than look deaf."""
        if not getattr(self, "_in_call", ""):
            return
        if any(pcm):
            self._call_silence = 0.0
            if self._call_silence_told:
                self._call_silence_told = False
                self._print("[the microphone is back during the call - listening for the wake word]")
            return
        self._call_silence += len(pcm) / 32000
        if self._call_silence > 4.0 and not self._call_silence_told:
            self._call_silence_told = True
            self._print(f"[{self._in_call} has the microphone to itself - Mint can't hear until the call ends]")
            self._state(self._idle_state(), f"{self._in_call} has the mic to itself")

    def _offer_meeting_notes(self, app: str) -> None:
        """A call started: the orb turns into the recorder (island.py) - Settings > Storage & Privacy
        turns the offer off. Mint has let go of the mic for the call, so it is one click, not a word."""
        from mint.tools import meetings
        from mint.core import prefs
        meetings.call_started(app)
        if not prefs.get("meeting_offer") or meetings.is_recording():
            return
        if time.monotonic() - getattr(self, "_meeting_offered", -1e9) < 1800:
            return
        self._meeting_offered = time.monotonic()
        # The orb itself turns into the recorder (island.py); no notification on top of it.
        self.ui.action("On a call? Tap ● to record it")

    def _take_mic_back(self) -> None:
        name, self._mic_lent_to = self._mic_lent_to, ""
        try:
            from mint.tools import meetings
            meetings.call_ended()
        except Exception:
            pass
        threading.Thread(target=self.audio.resume, daemon=True).start()
        self._print(f"[{name} is done with the microphone - Mint is listening again]")
        self._state(self._idle_state())

    def set_voice(self, on: bool) -> None:
        """Spoken replies on or off. Any thread."""
        self.voice_on = on
        self._print("[spoken replies on]" if on else "[silent: replies on screen only]")
        if not on and self.loop is not None:
            self.loop.call_soon_threadsafe(self._flush_playback)

    def dismiss(self) -> None:
        """Close from the HUD or a shortcut: stop talking, stop listening. Loop thread."""
        self._flush_playback()
        self.ui.close_console()
        if self.hands_free:
            self.go_to_sleep("dismissed")
        self._state(self._idle_state())

    def _print(self, text: str) -> None:
        print(f"  {time.strftime('%H:%M:%S')} {text}", flush=True)

    # --- microphone routing ----------------------------------------------------

    async def _on_audio(self, pcm: bytes, from_ear: bool = False) -> None:
        for probe in list(getattr(self, "_mic_probes", ())):
            try:
                probe(pcm)                        # the mic test hears exactly what Mint hears
            except Exception:
                log.debug("mic probe failed", exc_info=True)
        if getattr(self, "_mic_testing", False):
            return                                # the mic test is listening; the user is not talking to Mint
        if self.meet is not None:
            return                                # on a Google Meet call, the call is the microphone (_on_meet_audio)
        self._call_mic_check(pcm)
        if self._ear_feeding and not from_ear:
            # Mint Ear is still handing over what it heard while this app
            # started; our own microphone takes over once that has caught up.
            self._mic_alive = True
            return
        from mint.voice import dictation
        from mint.voice import wake_train
        dictation.ring(pcm)                       # the half second before a dictation / hold-to-talk key
        if dictation.talk.on:
            dictation.talk.feed(pcm)              # hold-to-talk: recorded, sent when the keys are let go
            return
        if wake_train.capturing():
            wake_train.feed(pcm)                  # Settings ▸ Microphone & voice is recording a take
            return
        if dictation.capturing():
            # The user is dictating text (dictation.py): the words are theirs to type, not a
            # request - Mint neither hears nor answers them.
            dictation.feed(pcm)
            return
        if self.paused:
            return
        if self.enroller is not None:
            # Training the voice: the microphone is the enrolment's alone.
            from mint.voice.wake import rms
            level = rms(pcm)
            self.ui.set_level(level)
            self.enroller.feed(pcm, level)
            return
        if self.asleep:
            if self._pending_wake:
                await self._pending_wake_audio(pcm)
                return
            self._preroll.add(pcm)
            fired = self._wake.heard(pcm)
            self._near_miss(fired)
            if fired and self._wake_is_user():
                await self.wake_up("wake word")
            return

        if self._busy and not self._heard_during_work and not self.listen_while_working:
            # While a tool runs, the room is not the user. Seen in testing: a
            # conversation nearby was transcribed as a new request mid-action,
            # and the model redid work that had already succeeded. So during a
            # tool, audio goes only to the wake word model; saying the wake word
            # reopens the stream for the user to cut in.
            if self._wake is not None:
                self._preroll.add(pcm)
                if self._wake.heard(pcm) and self._wake_is_user():
                    self._heard_during_work = True
                    self._next_kind = "wake"
                    self._print("[wake word while working - listening]")
                    tail = self._after_wake_phrase(self._preroll.drain())
                    if tail:
                        await self.out_queue.put(tail)
            return

        from mint.voice.wake import rms
        level = rms(pcm)
        self.ui.set_level(level)
        if self._gate is not None:
            # Voice lock: only the user's speech goes out; the start of each
            # utterance is held until it is verified (see voicelock.Gate).
            out, event = self._gate.feed(pcm, level)
            if event:
                self._gate_event(event)
            # Only the user's voice (or speech still being checked) holds the window open.
            self._window.sound(time.monotonic(), self._gate.state in ("pending", "open")
                               and level > self._gate.speech_threshold())
            if self._gate.state == "open" and level > self._gate.speech_threshold():
                self._last_voice = time.monotonic()
                self._turn_levels.append(level)
            if out:
                try:
                    self.out_queue.put_nowait(out)
                except asyncio.QueueFull:
                    log.debug("audio queue full; dropping a frame")
            return
        if level > 0.012:
            self._last_voice = time.monotonic()
        self._window.sound(time.monotonic(), level > max(0.012, self._noise_floor * 4))
        self._barge_in_check(level)
        try:
            self.out_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            log.debug("audio queue full; dropping a frame")

    def _gate_event(self, event: str) -> None:
        """React to the voice lock: talking over Mint, and ignored voices."""
        duck = getattr(self.audio, "duck", None)
        if event == "pending":
            if self.audio.playing and not self.half_duplex and duck:
                duck(True)               # someone is talking over Mint: quieter while we check
            return
        if duck:
            duck(False)
        if event.startswith("open"):
            self._users_words = not event.endswith("end")
            if self.audio.playing and not self.half_duplex:
                self._flush_playback()
                self._print(f"[you interrupted - listening] (voice {self._gate.last_score:.2f})")
                self._window.hold(time.monotonic())
                self._state("awake")
            if event.endswith("end"):
                self._voice_sent(time.monotonic())
        elif event == "end" and not self.asleep and getattr(self, "_users_words", False):
            self._users_words = False
            self._voice_sent(time.monotonic())        # the user's verified words just finished
        elif event.startswith("closed"):
            if getattr(self, "_users_words", False) and self.session is not None and self.loop is not None:
                # The voice lock stopped the audio mid-utterance: Gemini never hears the end of it.
                self._watch_answer(time.monotonic())
            self._users_words = False
            self._window.other_voice()
            now = time.monotonic()
            if now - self._ignored_at > 4:
                self._print(f"[ignored another voice] (voice {self._gate.last_score:.2f})")
            self._ignored_at = now

    def _voice_sent(self, now: float) -> None:
        """The user finished saying something that went to Gemini: watch that something comes back."""
        if self.loop is None or self.session is None:
            return
        self._spoke_at = now
        self._asked_at = now
        self._converse(True)                 # the user said their request: echo cancellation, while the model thinks
        self._watch_answer(now)
        self._watch_stall(now)
        if getattr(self, "_voice_watch_task", None) is None or self._voice_watch_task.done():
            self._voice_watch_task = self.loop.create_task(self._voice_watch())

    def _note_latency(self, now: float) -> None:
        """The model began answering: how long after the user's request (their voice ending, or the typed
        text). 6 Oct evening: gemini-3.8-live took 1-4 s with 15-17 s stalls (even with a bare config) while
        gemini-3.1-flash-live answered in 0.55-0.85 s every time. Slow several times running: use the fast one."""
        asked = getattr(self, "_asked_at", 0.0)
        if not asked:
            return
        self._asked_at = 0.0
        took = max(0.0, now - asked)
        recent = getattr(self, "_latencies", None)
        if recent is None or getattr(self, "_latency_model", "") != config.MODEL:
            recent = self._latencies = []
            self._latency_model = config.MODEL
        recent.append(took)
        del recent[:-6]
        log.info("answer began %.2f s after the request (%s)", took, config.MODEL)
        from mint.voice import live_models
        if took <= SLOW_MEDIAN:
            live_models.good(config.MODEL)
        why = _too_slow(recent)
        if why:
            self._move_model(why, strike=True)

    def _move_model(self, why: str, strike: bool = True, at_once: bool = False) -> None:
        """This model is misbehaving (strike) or needs a rest: use the next good one (live_models)."""
        from mint.voice import live_models
        from mint.core import prefs
        if self.loop is None or prefs.get("switch_when_slow") is False or getattr(self, "_speed_switching", False):
            return
        if strike:
            live_models.strike(config.MODEL, why)
        if live_models.choose(config.MODEL) == config.MODEL:
            return                                  # nowhere better to go
        self._speed_switching = True
        self.loop.create_task(self._switch_model(why, at_once=at_once))

    async def _promise_watch(self, request: str, said_at: float, epoch: int) -> None:
        """6 Oct (tested by the parallel-tasks session): gemini-3.8-live-extended-thinking said "I'll start that
        research", ended its turn as still in progress - and in 2 of 3 runs never made the tool call. A promise
        with no tool call in PROMISE_WAIT s: that model is benched and the request goes to the next one."""
        try:
            await asyncio.sleep(PROMISE_WAIT)
            if (getattr(self, "_tool_call_at", 0.0) > said_at or epoch != self._stop_epoch or self._busy
                    or self.session is None):
                return
            from mint.voice import live_models
            self._print(f"[{live_models.label(config.MODEL)} said it would do it but didn't start - asking the "
                        f"next model: {request[:80]}]")
            live_models.strike(config.MODEL, "promised an action, made no tool call")
            self._unanswered = request
            self._move_model("promised an action, made no tool call", strike=False, at_once=True)
        except Exception:
            log.debug("the promise watch failed", exc_info=True)

    def _watch_stall(self, asked: float) -> None:
        if self.loop is not None and asked:
            self.loop.create_task(self._stall_watch(asked))

    async def _stall_watch(self, asked: float) -> None:
        """A request with nothing at all back after SLOW_STALL seconds (23:18 on 6 Oct: 30 s of silence from
        gemini-3.8-live right after connecting): the user is waiting now. Move to the next model at once and
        ask it the same thing."""
        try:
            await asyncio.sleep(SLOW_STALL)
            if self._asked_at != asked or self.session is None or self._busy:
                return
            if not self._unanswered:
                words = self._heard_words()
                if words and getattr(self, "_after_wake", False):
                    self._unanswered = words           # asked again on the new connection (run)
            self._move_model(f"no answer in {SLOW_STALL:g} s", strike=True, at_once=True)
        except Exception:
            log.debug("the stall watch failed", exc_info=True)

    def _move_key(self, why: str) -> None:
        """Reconnect on the same model with the next Gemini key (gemini_keys.live_key skips the one set aside) - at a
        quiet moment, carrying the job on if one is under way."""
        if self.loop is None or getattr(self, "_speed_switching", False):
            return
        self._speed_switching = True

        async def go():
            from mint.core import gemini_keys
            try:
                await self._quiet_for(1.5, time.monotonic() + 60)
                session = self.session
                if session is None:
                    return
                nxt = gemini_keys.live_key()[0]
                self._print(f"[Gemini {gemini_keys.label(_live_env)}: {why} - the voice moves to {gemini_keys.label(nxt)}]")
                if self._job_open():
                    self._carry_note = _carry_on_note(self)
                self._resume_handle = None          # a resumption handle may not carry across keys
                self._restarting = True
                await session.close()
            except Exception:
                log.debug("moving to the next Gemini key failed", exc_info=True)
            finally:
                self._speed_switching = False
        self.loop.create_task(go())

    async def _switch_model(self, why: str, at_once: bool = False) -> None:
        """Reconnect on the model live_models chooses - at a quiet moment (never mid-reply), or at once when the
        user is waiting on a request that got nothing back. A fresh session: the prompt carries the last turns
        and memory, and an unanswered request is sent again (run)."""
        from mint.voice import live_models
        try:
            if not at_once:
                await self._quiet_for(1.5, time.monotonic() + 120)
            current, session = config.MODEL, self.session
            target = live_models.choose(current)
            if target == current or session is None:
                return
            self._print(f"[{live_models.label(current)}: {why} - switching to {live_models.label(target)}]")
            log.info("switching voice model %s -> %s (%s)", current, target, why)
            if self._job_open():
                # Mid-job (a per-minute token limit can strike between two clicks): the new session picks the job up
                # where it was, instead of waiting to be asked again.
                self._carry_note = _carry_on_note(self)
            config.MODEL = target
            self._latencies = []
            self._resume_handle = None             # a resumption handle belongs to the other model
            self._restarting = True
            await session.close()
        except Exception:
            log.debug("switching the voice model failed", exc_info=True)
        finally:
            self._speed_switching = False

    async def _prefer_watch(self) -> None:
        """Once a better model is off the bench, move back to it - while Mint is asleep, never mid-conversation."""
        from mint.voice import live_models
        live_models.discover_later()                 # which Live models this key has (daily)
        looked = time.monotonic()
        while True:
            await asyncio.sleep(60)
            if time.monotonic() - looked > 3600:
                live_models.discover_later()
                looked = time.monotonic()
            try:
                better = live_models.better(config.MODEL)
            except Exception:
                log.debug("checking the voice models failed", exc_info=True)
                continue
            if (better and self.session is not None and self.asleep and not self._busy
                    and not getattr(self, "_speed_switching", False)):
                self._speed_switching = True
                await self._switch_model(f"{live_models.label(better)} is ready again", at_once=True)

    def _heard_words(self) -> str:
        """The user's words in this turn not yet answered or dropped ('' if none)."""
        if self._dropping or self._suppress_turn:
            return ""
        return " ".join((self._heard or "")[getattr(self, "_seg", 0):].split())

    def _answered(self) -> bool:
        """The model has begun answering this turn (sound, words, a tool call), or the words were judged."""
        return (getattr(self, "_model_active_at", 0.0) >= getattr(self, "_turn_heard_at", math.inf)
                or self._verdict is not None)

    def _watch_answer(self, spoke: float) -> None:
        task = getattr(self, "_answer_task", None)
        if task is not None and not task.done():
            task.cancel()                       # newer words: watch from them
        self._answer_task = self.loop.create_task(self._voice_answer_watch(spoke))

    def _answer_pending(self) -> bool:
        task = getattr(self, "_answer_task", None)
        return task is not None and not task.done()

    async def _voice_answer_watch(self, spoke: float) -> None:
        """6 Oct: "Tell me what skills you have" - the transcript showed, then nothing; Mint slept, or answered
        much later. Gemini had the words but never heard them end (the voice lock had cut the audio mid-word, or
        noise kept its voice detection open), so it waited. Close the user's audio soon after their voice stops;
        still nothing, and they had just said "Hey Mint", send their words as text. Typed turns, a judged turn and
        any answer at all end the watch."""
        try:
            await asyncio.sleep(ANSWER_NUDGE)
            if self._answered() or not self._heard_words() or self.session is None or self._typed_turn:
                return
            log.info("no answer %.1f s after the user's words: closing their audio", ANSWER_NUDGE)
            self._print("[no answer yet - telling the voice service you're done]")
            await self._close_user_audio()
            await asyncio.sleep(ANSWER_RESEND)
            words = self._heard_words()
            if self._answered() or not words or self.asleep or self._typed_turn:
                return
            if getattr(self, "_after_wake", False):
                self._print(f"[still no answer - sending your words as text: {words[:80]}]")
                asked = getattr(self, "_asked_at", 0.0)
                await self.inject_text(words)
                self._asked_at = asked or self._asked_at   # the wait is timed from the spoken words
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("the answer watch failed", exc_info=True)

    async def _voice_watch(self, wait: float = 14.0) -> None:
        """Spoken words that get nothing at all back - not even their transcript - within `wait` seconds: the
        session is deaf (5 Oct: gemini-3.8-live failing server-side; "Hey Mint" and a whole request went
        unanswered and Mint just fell asleep). Reconnect fresh, move to the fallback model if it happens again,
        and tell the user to say it again - nothing they said is lost silently."""
        while True:
            spoke = self._spoke_at
            await asyncio.sleep(max(0.5, spoke + wait - time.monotonic()))
            if self._spoke_at > spoke:
                continue                       # they said more since: watch from the newest words
            break
        if (getattr(self, "_model_active_at", 0.0) > spoke or self.session is None or self._busy
                or self.audio.playing):
            self._deaf_voice = 0
            return
        self._deaf_voice = getattr(self, "_deaf_voice", 0) + 1
        from mint.voice import live_models
        live_models.strike(config.MODEL, f"no answer to the user's voice in {wait:.0f} s")
        primary, config.MODEL = config.MODEL, live_models.choose(config.MODEL)
        if config.MODEL != primary:
            self._print(f"[{live_models.label(primary)} is not answering; using {live_models.label(config.MODEL)}]")
        words = self._heard_words()
        if words and getattr(self, "_after_wake", False):
            self._unanswered = words           # sent again on the new connection (run)
            self._print(f"[no answer to your voice in {wait:.0f} s: reconnecting - sending your words again]")
        else:
            self._print(f"[no answer to your voice in {wait:.0f} s: reconnecting - say it again]")
            try:
                self.ui.assistant_said("Sorry, the voice service didn't answer. Say that again?")
            except Exception:
                pass
        self._resume_handle = None
        try:
            await self.session.close()
        except Exception:
            log.debug("closing the deaf session", exc_info=True)

    async def _pending_wake_audio(self, pcm: bytes) -> None:
        """A "Hey Mint" the voice check was unsure of: decide on what follows."""
        from mint.voice.wake import rms
        out, event = self._gate.feed(pcm, rms(pcm))
        if event and event.startswith("open"):
            self._pending_wake = 0.0
            self._print(f"[Hey Mint - voice confirmed ({self._gate.last_score:.2f})]")
            await self.wake_up("wake word (confirmed)")
            out, self._wake_cut = out[self._wake_cut:], 0     # what followed the phrase
            if out and self.session is not None:
                await self.out_queue.put(out)
            return
        if (event and (event.startswith("closed") or event == "end")) or time.monotonic() - self._pending_wake > 4.0:
            self._pending_wake = 0.0
            self._gate.cancel()
            self._wake_refused_at = time.monotonic()          # a second "Hey Mint" soon after gets in
            self._print(f"[Hey Mint in another voice - ignored] (voice {self._gate.last_score:.2f})")

    def _after_wake_phrase(self, pcm: bytes) -> bytes:
        """The pre-roll without the wake phrase. Sent to Gemini, "Hey Mint" came
        back as "payment" nearly every time (26-27 Sep) and the model answered
        that; the phrase was already recognised here, Gemini needs only what
        follows. See wake.phrase_cut."""
        cut = wake_phrase_cut(pcm, getattr(self._wake, "phrase_end_lag", None))
        if cut:
            log.info("wake phrase cut: %.2f s dropped, %.2f s kept", cut / 32000, (len(pcm) - cut) / 32000)
        return pcm[cut:]

    def _near_miss(self, fired: bool) -> None:
        """Something close to the wake word that did not wake Mint: one log line per attempt, with its score -
        so "I said Hey Alex ten times and nothing" can be told from "it never heard me" (8 Oct)."""
        peak = float(getattr(self._wake, "last_peak", 0.0) or 0.0)
        best = getattr(self, "_near_best", 0.0)
        if fired:
            self._near_best = 0.0
        elif peak > 0.35:
            self._near_best = max(best, peak)
        elif best and peak < 0.15:
            self._near_best = 0.0
            from mint.voice.wake import display_phrase
            need = float(getattr(self._wake, "threshold", 0.85) or 0.85)
            self._print(f"[heard something like “{display_phrase()}” - score {best:.2f}, needs {need:.2f}]")

    @staticmethod
    def _quiet_asleep_wanted(lock_on: bool | None = None) -> bool:
        """The plain microphone while waiting for the wake word: the user's choice (quiet_while_waiting),
        unless the voice lock is on - it needs the echo-cancelled microphone its voiceprint was recorded with."""
        from mint.core import prefs
        from mint.voice import voicelock
        if lock_on is None:
            lock_on = bool(prefs.get("voice_lock")) and voicelock.lock.enrolled
        return prefs.get("quiet_while_waiting") is not False and not lock_on

    def _wake_is_user(self) -> bool:
        """With the voice lock on, only the user's "Hey Mint" wakes Mint.

        Clear yes: wake now. Clear no: ignore. In between: hold the phrase and
        let the gate decide on the next second of speech ("Hey Mint, open
        Slack" gives it plenty); see the pending-wake branch in _on_audio."""
        if self._gate is None:
            return True
        if self._in_call:
            # During a call the mic is the plain one (no echo cancellation beside the call's), which the voiceprint
            # was not recorded with: the wake word alone wakes Mint, and the user is right there.
            self._print(f"[Hey Mint during the {self._in_call} call - voice check skipped]")
            return True
        from mint.voice import voicelock
        audio = voicelock.voiced(voicelock.to_float(self._preroll.peek()),
                                 floor=self._gate.speech_threshold())
        verdict, phrase, voice = voicelock.lock.wake_verdict(audio)
        mic = ", plain mic" if voicelock.capture_shift else ""       # to tune voicelock.PLAIN_MIC from real wakes
        if verdict == "yes":
            log.info("wake word voice check: phrase %.2f voice %.2f%s", phrase, voice, mic)
            return True
        now = time.monotonic()
        if verdict != "yes" and now - self._wake_refused_at < 15 and phrase > 0.05:
            # The same "Hey Mint" again within seconds, after the voice check refused it: someone is trying
            # to reach Mint and being ignored. An impostor rarely repeats the wake word; the user locked out
            # of their own assistant is worse (7-8 Oct: dozens of refusals in a row). Let the second one in.
            self._wake_refused_at = 0.0
            self._print(f"[Hey Mint again - letting you in (phrase {phrase:.2f}, voice {voice:.2f}{mic})]")
            return True
        if verdict == "maybe":
            self._pending_wake = time.monotonic()
            held = self._preroll.drain()
            # The phrase stays in what the gate judges (it is voice evidence);
            # it is cut from what is sent once the voice is confirmed.
            self._wake_cut = wake_phrase_cut(held, getattr(self._wake, "phrase_end_lag", None))
            self._gate.begin_pending(held)
            self._print(f"[Hey Mint - checking the voice (phrase {phrase:.2f}, voice {voice:.2f}{mic})]")
            return False
        self._print(f"[Hey Mint in another voice - ignored] (phrase {phrase:.2f}, voice {voice:.2f}{mic})")
        self._wake_refused_at = now
        self._preroll.drain()
        return False

    def _barge_in_check(self, level: float) -> None:
        """Stop talking the moment the user talks over Mint.

        With echo cancellation the mic hears the user, not Mint's own voice,
        so sustained loudness while Mint is speaking means the user wants the
        floor. Cut playback here at once - Gemini's server also notices and
        stops generating, but waiting for that round trip feels sluggish.
        """
        if self.half_duplex or not self.audio.playing:
            # Learn the room's noise level while nobody is speaking.
            if not self.audio.playing and level < 0.03:
                self._noise_floor = 0.98 * self._noise_floor + 0.02 * level
            self._barge_frames = 0
            return
        threshold = max(0.02, self._noise_floor * 5)
        self._barge_frames = self._barge_frames + 1 if level > threshold else 0
        if self._barge_frames >= 3:          # ~0.3 s of speech, not a cough
            self._barge_frames = 0
            self._flush_playback()
            self._print("[you interrupted - listening]")
            self._window.hold(time.monotonic())
            self._state("awake")

    def _on_play(self, pcm: bytes) -> None:
        """Every chunk of Mint's voice, as it is played. Drives the orb."""
        from mint.voice.wake import rms
        self.ui.set_level(rms(pcm))
        if self.meet is not None:
            self.meet.speak(pcm)                  # to the call (the Mac's speaker plays it silently, for the timing)

    # --- Google Meet calls (meet_call.py) -------------------------------------------------------------------

    def meet_begin(self, call) -> None:
        """Any thread: a call is on. Mint hears the call instead of the Mac's mic and talks into it."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._meet_begin, call)

    def _meet_begin(self, call) -> None:
        self.meet = call
        self._flush_playback()
        if self._gate is not None:
            self._gate.cancel()
        silent = getattr(self.audio, "set_silent", None)
        if silent is not None:
            silent(True)
        self.audio.muted = False
        # The Mac's microphone may be off (Mint "paused"): the call is the microphone now, so Mint listens to it for
        # as long as it lasts (6 Oct: paused, it dropped everything said in the call). The pause comes back after.
        self._meet_was_paused = self.paused
        if self.paused:
            self.set_paused(False)
        self._print("[in a Google Meet call: listening to the call, talking into it]")
        if self.asleep:
            self.loop.create_task(self.wake_up("meet"))

    def meet_end(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._meet_end)

    def _meet_end(self) -> None:
        if self.meet is None:
            return
        self._flush_playback()
        self.meet = None
        silent = getattr(self.audio, "set_silent", None)
        if silent is not None:
            silent(False)
        while not self._meet_q.empty():
            self._meet_q.get_nowait()
        self._print("[the Google Meet call is over: back to the Mac's microphone]")
        if getattr(self, "_meet_was_paused", False):
            self._meet_was_paused = False
            self.set_paused(True)
        self._last_voice = time.monotonic()
        self._window.finished(self._last_voice)

    def meet_audio(self, pcm: bytes) -> None:
        """Any thread: 16 kHz audio from the call."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._meet_offer, pcm)

    def _meet_offer(self, pcm: bytes) -> None:
        try:
            self._meet_q.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    async def _meet_feed(self) -> None:
        while True:
            pcm = await self._meet_q.get()
            if self.meet is not None:
                await self._on_meet_audio(pcm)

    async def _on_meet_audio(self, pcm: bytes) -> None:
        """The call's audio: only the user is in it, so no wake word, no voice lock, and Mint may be talked to while
        it works (Meet sends the others' audio, never Mint's own, so there's no echo)."""
        if self.paused:
            return
        if self.asleep:
            await self.wake_up("meet")
        from mint.voice.wake import rms
        level = rms(pcm)
        now = time.monotonic()
        if not self.audio.playing:
            self.ui.set_level(level)
        if level > 0.012:
            self._last_voice = now
        self._window.sound(now, level > 0.012)
        try:
            self.out_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            log.debug("audio queue full; dropping a call frame")

    def _meet_request(self, spoken: bool = False) -> None:
        """On the loop: start the Google Meet the user asked for; the model only says so, then tells the outcome.
        Once Mint is in the call its voice is the call's, so success is told by a notification and Telegram
        (meet_call.start) rather than spoken here."""
        from mint.app import meet_call
        if spoken:
            self._flush_playback()                      # whatever it began to say from an earlier attempt
            self.meet_note(meet_call.START_NOTE)

        async def run() -> None:
            said = await asyncio.to_thread(meet_call.start)
            call = meet_call.current()
            self._print(f"[google_meet] {said}")
            if call is None or not call.live:
                for _ in range(40):              # after "Starting the Google Meet." is said: a note mid-turn was lost
                    if not self._turn_open and not self.audio.playing and self.audio_in.empty():
                        break
                    await asyncio.sleep(0.25)
                self.meet_note(meet_call.started_note(said))
        self.loop.create_task(run())

    def meet_chat(self, text: str) -> None:
        """Any thread: a message in the call's chat - a request, as if typed. The answer goes back to the chat too
        (and is said in the call)."""
        if self.loop is None:
            return

        def go() -> None:
            self._chat_reply, self._chat_until = [], time.monotonic() + 120
            self.loop.create_task(self.inject_text(text))
        self.loop.call_soon_threadsafe(go)

    def _chat_flush(self) -> None:
        """At the end of a model turn: what Mint said, to the call's chat (meet_chat)."""
        words = "".join(getattr(self, "_chat_reply", None) or []).strip()
        if getattr(self, "_chat_reply", None) is None:
            return
        if time.monotonic() > getattr(self, "_chat_until", 0):
            self._chat_reply = None
            return
        self._chat_reply = []
        call = self.meet
        if words and call is not None:
            threading.Thread(target=call.chat_send, args=(words,), name="meet-chat", daemon=True).start()

    def meet_note(self, text: str) -> None:
        """Any thread: tell the model something about the call (it answers it, like a request)."""
        if self.loop is None:
            return

        def send() -> None:
            if self.session is None:
                return
            self._turn_open = True
            self._window.handling()
            self.loop.create_task(self.session.send_realtime_input(text=text))
        self.loop.call_soon_threadsafe(send)

    STALE_AFTER = 11 * 60     # a connection asleep this long is renewed before the next wake

    def _renew_if_stale(self) -> None:
        """Asleep on a connection opened over STALE_AFTER ago: renew it (resumed - same conversation) before anyone
        needs it. Seen 10 Oct: woken at 01:14 on a connection from 00:57 that had gone quiet without closing, the
        user's words were never transcribed and Mint fell asleep "nothing said after the wake word"."""
        connected = getattr(self, "_connected_at", 0.0)
        if (not connected or self.session is None or self._busy or getattr(self, "_restarting", False)
                or getattr(self, "_speed_switching", False)):
            return
        now = time.monotonic()
        if now - connected < self.STALE_AFTER or now - max(self._server_at, self._tools_at) < 60:
            return
        self._connected_at = now                # once per connection
        self._print("[renewing the voice connection while asleep]")
        self._restarting = True                 # (resumed with its handle: the conversation carries on)

        async def close(session=self.session):
            try:
                await session.close()
            except Exception:
                log.debug("closing a stale session", exc_info=True)
        self.loop.create_task(close())

    async def _idle_watch(self) -> None:
        """Fall asleep once the listening window has passed (listening.Window): a few seconds after
        Mint is done, unless work is in progress, Mint is speaking, or the user's words are still
        being judged. Talk that was not for Mint does not keep it open."""
        muted_since = 0.0
        while True:
            await asyncio.sleep(0.5)
            # The mic closes while Mint speaks (no echo cancellation) and opens when playback drains. A rebuild of
            # the audio engine mid-reply dropped the rest of the reply without a "drained", and the mic stayed
            # closed for good: 8 Oct, Mint deaf after its first answer, the mic test "No sound at all".
            if getattr(self.audio, "muted", False) and not self.audio.playing and self.audio_in.empty():
                muted_since = muted_since or time.monotonic()
                if time.monotonic() - muted_since > 1.5:
                    self.audio.muted = False
                    muted_since = 0.0
                    self._print("[the microphone was left closed after Mint spoke - open again]")
            else:
                muted_since = 0.0
            if self.asleep and not self.paused:
                self._renew_if_stale()
            if self.asleep or self.paused or not self.hands_free:
                continue
            now = time.monotonic()
            playing = self.audio.playing or not self.audio_in.empty()
            if playing:
                self._last_voice = now
            # Words waiting for their verdict (bounded: a turn the model never answers ends too).
            deciding = bool(self._held) or self._answer_pending() or (
                self._verdict_pending() and now - self._last_chunk_at < listening.JUDGE_WAIT)
            idle_for = now - max(self._server_at, self._tools_at)
            # A planned task with steps left is still work between tool batches (the model thinking, the
            # autopilot nudging it on): on 5 Oct Mint fell asleep mid-task, 8 s into "install an extension".
            from mint.app import tasks
            planned = self.task is not None and tasks.current(self.task) is not None and idle_for < 90
            if self._window.should_sleep(now, busy=self._busy or planned, playing=playing, deciding=deciding,
                                         idle_for=idle_for):
                self.go_to_sleep(self._window.why())

    # --- outbound --------------------------------------------------------------

    async def _send_audio(self) -> None:
        while True:
            pcm = await self.out_queue.get()
            if self.session is None:
                continue
            try:
                await self.session.send_realtime_input(
                    audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))
            except Exception:
                log.debug("send failed; the session is probably reconnecting")

    async def inject_text(self, text: str, asked: str | None = None) -> None:
        """Send a typed request, as if spoken. Used by the terminal, `mint --say`, and timers."""
        if not text.strip():
            return
        from mint.core import guard
        if guard.current() is not None and guard.heard(text):
            self.ui.user_said(text, new_turn=True)     # "yes" / "no" to the guard's open question: that's all it was
            return
        self._last_active = time.monotonic()
        self._ever_awake = True
        self._hush = False
        self._farewell_bytes = -1
        if self.session is None:
            # Reconnecting (a voice change, a network drop): in testing a
            # request typed then was silently lost. Keep it for the new session.
            self._pending_text.append(text)
            self._print(f"[typed while reconnecting - sending once connected: {text[:80]}]")
            return
        from mint.app import control
        from mint.core import prefs
        if control.is_stop(text, prefs.get("stop_words")) and len(text.split()) <= 3:
            self.ui.user_said(text, new_turn=True)
            await self.stop_everything("typed")
            return
        # A typed request does not open the microphone: the user is typing,
        # and an open mic only invites room noise in as a second request. They
        # can still say the wake word to follow up by voice.
        self._state("thinking")
        self.ui.new_exchange()
        self.ui.user_said(text, new_turn=True)
        from mint.knowledge.conversation import memory
        memory.add("user", text)
        if not text.lstrip().startswith(("(System", "[")):        # a timer's or autopilot's note is not the user
            lines = self.__dict__.setdefault("_user_lines", [])
            lines.append((time.time(), " ".join(text.split())[:240]))
            del lines[:-6]
        try:
            from mint.app import live
            live.typed(asked or text)       # the user's own words decide what they asked (not a file name)
        except ImportError:
            pass
        self._turn_open = True
        self._suppress_turn = False
        self._reset_verdict()
        self._voice_turn = False              # typed on purpose: always answered (speech after it is judged)
        self._window.handling()
        self._typed_turn = True               # typed on purpose: always answered
        self._filler_checked = True
        self._halted = False                  # a new request: tools may run again
        self._print(f"[typed: {text[:120]}]")
        self._asked_at = time.monotonic()
        self._typed_request = text
        from mint.app import telegram
        telegram.on_event("request", {"text": text})    # its own request from the phone, or one typed here
        # Close any open stretch of microphone audio first: in testing, a typed
        # request sent while the mic was streaming room noise went unanswered -
        # the server was still waiting for that "speech" to end.
        if not self.asleep and not self.paused:
            await self._end_audio_stream()
        else:
            await self._close_user_audio()
        self._unanswered = text
        if self.loop is not None:
            self.loop.create_task(self._answer_watch(text, time.monotonic(), self._stop_epoch))
            self._watch_stall(self._asked_at)
        from mint.app import instant
        from mint.app import meet_call
        if meet_call.wants_end(text):
            instant.ran("google_meet", {"action": "end"}, text)
            threading.Thread(target=meet_call.end, name="meet-end", daemon=True).start()
            text = f"{text}\n{meet_call.END_NOTE}"
        elif meet_call.wants_call(text):
            # Started here, not left to the model (it once answered from an earlier failed attempt).
            instant.ran("google_meet", {"action": "start"}, text)
            self._meet_request()
            text = f"{text}\n{meet_call.START_NOTE}"
        if len(text.split()) >= 3:
            # What Mint has saved that bears on it, sent with the words (voice requests get theirs with the
            # first tool, or by recall): a typed or remote request is answered from what Mint knows.
            try:
                from mint.knowledge import memory as membank
                memo = await asyncio.wait_for(asyncio.to_thread(membank.recall_block, text, 4), 1.5)
            except Exception:
                memo = ""
            if memo:
                text = f"{text}\n\n[{memo}]"
        from mint.app import background
        jobs = background.running_note()
        if jobs and _ASKS_PROGRESS.search(text):
            # "What is it doing?" while a job runs: answer from the job's real state, never a guess. Seen 10 Oct:
            # asked twice, Mint said "it's processing the images" while the job sat "waiting for you to pause typing".
            text = (f"{text}\n\n[{jobs} Answer from this, in plain words - say if it is waiting or stuck and why; "
                    "don't make up progress.]")
        session = self.session
        try:
            # Realtime text is how the 3.x Live models take a typed turn
            # mid-conversation; client content is meant for seeding history.
            await session.send_realtime_input(text=text)
        except Exception as error:
            log.info("realtime text refused (%s); sending as client content", error)
            try:
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=text)]),
                    turn_complete=True)
            except Exception:
                # The session closed under us: send it on the next one.
                self._pending_text.append(text)
                self._print("[typed while reconnecting - sending once connected]")

    def _check_empty_done(self, said: str) -> None:
        """A new request answered with just "All done." and nothing done: after a few autopilot nudges ("if it
        is all done, say 'All done.'") the model took the user's next requests for nudges too and did nothing
        (seen 30 Sep). Tell it once that this was the user."""
        nudge, self._nudge_turn = getattr(self, "_nudge_turn", False), False
        words = " ".join((said or "").lower().split()).rstrip(".! ")
        if nudge or self._suppress_turn or not words.startswith("all done") or len(words.split()) > 4:
            return
        from mint.app import autopilot
        if autopilot.tools_done() or getattr(self, "_empty_done_at", 0) > time.monotonic() - 60:
            return
        self._empty_done_at = time.monotonic()
        self._print("[the model said 'All done.' to a new request without doing it: telling it]")
        note = ("(Mint note - not the user: the user's last message was a NEW request, not an autopilot note. "
                "Nothing has been done for it yet. Do it now.)")
        if self.loop is not None and self.session is not None:
            self._turn_open = True
            self._window.handling()
            self.loop.create_task(self.session.send_realtime_input(text=note))

    def _check_refused_job(self, said: str) -> None:
        """The user asked for a background job and the model said it can't (see background.refused_job):
        correct it once, so it starts the job instead of repeating the refusal."""
        from mint.app import background
        from mint.app import live
        if not background.refused_job(live.request(), said):
            return
        if time.monotonic() - background.last_started < 30 or \
                getattr(self, "_refused_job_at", 0) > time.monotonic() - 60:
            return
        self._refused_job_at = time.monotonic()
        self._print("[the model said a background job can't use the screen: correcting it]")
        if self.loop is not None and self.session is not None:
            self._turn_open = True
            self._window.handling()
            self.loop.create_task(self.session.send_realtime_input(text=background.REFUSAL_NOTE))

    async def _answer_watch(self, text: str, sent_at: float, epoch: int, wait: float = 30.0) -> None:
        """A typed request that gets nothing at all back - no words, no tool call - within `wait`
        seconds: the session is connected but deaf. Seen after a voice-change reconnect (resumed with
        its handle): 13 minutes of requests went unanswered until the server dropped it. Reconnect
        with a fresh session (memory is kept) and send the request again."""
        await asyncio.sleep(wait)
        if epoch != self._stop_epoch or self._server_at > sent_at or self.session is None:
            return
        if self._tool_task is not None and not self._tool_task.done():
            return
        self._print(f"[no answer in {wait:.0f} s: reconnecting with a fresh session and sending it again]")
        self._resume_handle = None
        self._pending_text.append(text)
        try:
            await self.session.close()
        except Exception:
            log.debug("closing the deaf session", exc_info=True)

    async def _close_user_audio(self) -> None:
        """Silence, then end-of-stream: closes any stretch of user speech the
        server still has open. Needed when no microphone audio is flowing
        (asleep, paused): after the mic went off mid-sentence - or in a session
        resumed from one that was cut off mid-turn - a typed turn got no answer
        at all (bench: none in 40 s; with 0.5 s of silence first none either;
        with 1 s, answered in 1.2 s). 1.5 s for margin; it costs nothing."""
        session = self.session
        if session is None:
            return
        try:
            await session.send_realtime_input(
                audio=types.Blob(data=bytes(2 * int(16000 * 1.5)), mime_type="audio/pcm;rate=16000"))
            await session.send_realtime_input(audio_stream_end=True)
        except Exception:
            log.debug("closing the user's audio failed", exc_info=True)

    async def _send_carry_note(self) -> None:
        note, self._carry_note = getattr(self, "_carry_note", ""), ""
        await asyncio.sleep(0.6)
        if not note or self.session is None or control_stopped():
            return
        self._print("[carrying on with the job on the new voice model]")
        self._nudge_turn = True
        self._turn_open = True
        try:
            await self.session.send_realtime_input(text=note)
        except Exception:
            log.debug("carry-on note not sent", exc_info=True)
            self._turn_open = False

    async def _send_pending_text(self) -> None:
        await asyncio.sleep(0.5)
        pending, self._pending_text = self._pending_text, []
        for text in pending:
            await self.inject_text(text)

    async def _end_audio_stream(self) -> None:
        if self.session is None:
            return
        try:
            await self.session.send_realtime_input(audio_stream_end=True)
        except Exception:
            log.debug("audio_stream_end failed", exc_info=True)

    async def _read_terminal(self) -> None:
        """Typing in the terminal. Absent when there is no terminal (the app bundle)."""
        if not sys.stdin or not sys.stdin.isatty():
            return
        while True:
            try:
                line = await asyncio.to_thread(input, "")
            except EOFError:
                return
            if line.strip().lower() in {"q", "quit", "exit"}:
                raise asyncio.CancelledError("user asked to quit")
            await self.inject_text(line)

    # --- inbound ---------------------------------------------------------------

    async def _receive(self) -> None:
        while True:
            if self.session is None:
                await asyncio.sleep(0.1)
                continue
            async for response in self.session.receive():
                await self._handle(response)

    async def _handle(self, response) -> None:
        if getattr(response, "tool_call", None) is not None:
            self._turn_steps = getattr(self, "_turn_steps", 0) + 1
        if (meta := getattr(response, "usage_metadata", None)) is not None:
            from mint.core import usage
            usage.live(config.MODEL, meta)
            # A turn's usage adds up the prompt re-read at every tool-call step (billed tokens), so it is not the
            # context's size: 321k after 11 tool calls in a 30k context (9 Oct) set off compaction - Mint started
            # over from a summary and forgot what the user had just said. One step's share is the context.
            self._watch_context(meta, steps=getattr(self, "_turn_steps", 0) + 1)
            from mint.voice import live_models
            spent = getattr(meta, "prompt_token_count", None) or getattr(meta, "total_token_count", 0) or 0
            from mint.core import gemini_keys
            if live_models.tokens(config.MODEL, spent, key=_live_env, rest_model=False):
                # Near this key's input-tokens-a-minute limit for this model (where it starts stalling). The limit
                # is per key: another Gemini key carries on with the same model; else another model for a bit.
                if _live_env and gemini_keys.other_free(_live_env, "live"):
                    gemini_keys.bench(_live_env, 60, "near its per-minute token limit", model="live")
                    self._move_key("near its per-minute token limit")
                else:
                    live_models.rest(config.MODEL)
                    self._move_model("near its per-minute token limit", strike=False)
        if update := getattr(response, "session_resumption_update", None):
            if getattr(update, "resumable", False) and getattr(update, "new_handle", None):
                self._resume_handle = update.new_handle
            return
        if (going := getattr(response, "go_away", None)) is not None:
            self._go_away(going)
            return
        self._last_active = self._server_at = time.monotonic()
        self._unanswered = ""

        if response.data or response.tool_call is not None or (
                response.server_content is not None and response.server_content.output_transcription):
            self._model_active_at = time.monotonic()      # for the autopilot's "quiet for 2 s"
            if not self.asleep:
                self._converse(True)          # (no voice lock, so no end-of-words signal: the answer is the cue)
            self._note_latency(self._model_active_at)
            # The model is answering: the user's words are in by now, so this
            # is when to ask whether they were meant for Mint at all.
            self._start_addressee_check()

        if response.data and not self._suppress_turn and not self._hush and self._filler_turn():
            self._suppress_turn = True
            self._filler_hit = True
            self._flush_playback()
            self._print(f"(no reply needed: {' '.join(self._heard.split())})")
        if response.data and self._farewell_bytes >= 0:
            # The goodbye: one short line (~2 s of speech), then silence.
            self._farewell_bytes += len(response.data)
            if self._farewell_bytes > 24000 * 2 * 2:
                self._hush = True
                self._farewell_bytes = -1
        if response.data and (self._suppress_turn or self._hush or self._mute):
            pass                         # not for Mint, filler, or after goodbye: not played
        elif response.data and self._verdict_pending():
            # Was that for Mint? Nothing is said until it is known (about 0.4 s, follow-ups only).
            self._held.append(response.data)
        elif response.data:
            self._play_reply(response.data)

        server = response.server_content
        if server is not None:
            if server.input_transcription and server.input_transcription.text:
                await self._on_heard(server.input_transcription.text)

            if (server.output_transcription and server.output_transcription.text and not self._suppress_turn
                    and not self._hush and not self._mute):
                if self._verdict_pending():
                    self._held_words.append(server.output_transcription.text)
                elif self._leak_check(server.output_transcription.text):
                    pass                         # tool-call text, not words: not shown, not played
                else:
                    self._said += server.output_transcription.text
                    if server.output_transcription.text.strip():
                        self._tools_unspoken = 0
                    self.ui.assistant_said(server.output_transcription.text)
                    if getattr(self, "_chat_reply", None) is not None:
                        self._chat_reply.append(server.output_transcription.text)

            if server.interrupted:
                self._flush_playback()

            if server.turn_complete:
                self._turn_steps = 0
                if self.meet is not None:
                    self._chat_flush()
                if self._verdict_pending() and self._heard[self._seg:].strip():
                    self._start_addressee_check()
                    await self._await_verdict(3.5)
                self._halted = False
                self._check_goodbye()
                if self._farewell_bytes >= 0:
                    self._farewell_bytes = -1  # the goodbye line is done: quiet from here
                    self._hush = True
                from mint.knowledge.conversation import memory
                if self._suppress_turn:
                    # Someone else's conversation, or a "hmm": not the user's history.
                    if self._heard.strip() and not self._filler_hit:
                        self._print(f"(not for me: {' '.join(self._heard.split())})")
                else:
                    if self._heard.strip() or getattr(self, "_typed_request", ""):
                        self._rescues = 0              # a new request: it gets its own rescues
                    if self._heard.strip():
                        self._print(f"you:    {' '.join(self._heard.split())}")
                        memory.add("user", self._heard)
                        lines = self.__dict__.setdefault("_user_lines", [])
                        lines.append((time.time(), " ".join(self._heard.split())[:240]))
                        del lines[:-6]
                        from mint.knowledge import teach
                        if teach.recording():
                            teach.add_narration(self._heard)      # what the user says while showing Mint
                    if self._said.strip():
                        self._answered_at = time.monotonic()
                        self._print(f"mint: {' '.join(self._said.split())}")
                        from mint.app import telegram
                        telegram.on_event("reply", {"text": " ".join(self._said.split())})
                        memory.add("mint", self._said)
                        self._last_said = self._said
                leaked, self._leaked = getattr(self, "_leaked", False), False
                finished_said = "" if self._suppress_turn else self._said
                # Tools ran and the model ended its turn without a word for them: the user is still waiting.
                # Seen 10 Oct 17:43: find_files, then an empty turn, and Mint fell asleep 6 s later - "it died".
                unanswered = (not self._suppress_turn and not finished_said.strip()
                              and (getattr(self, "_tools_unspoken", 0) > 0 or leaked))
                request = " ".join((self._heard or getattr(self, "_typed_request", "") or "").split())
                self._typed_request = ""
                if (not self._suppress_turn and request and self.loop is not None
                        and "IN_PROGRESS" in str(getattr(server, "interaction_status", "") or "")):
                    # The model said it is on it ("I'll start that research") and ended its turn: a tool call
                    # should follow. Watch that one does (_promise_watch).
                    self.loop.create_task(self._promise_watch(request, time.monotonic(), self._stop_epoch))
                self._check_empty_done(finished_said)
                self._check_refused_job(finished_said)
                if unanswered and self.loop is not None:
                    self._window.handling()          # not done: no follow-up clock, the rescue watch decides
                    self.loop.create_task(self._silence_watch(self._stop_epoch, time.monotonic(), 5.0, leaked))
                elif (not self._suppress_turn or self._verdict == "act") and \
                        not (self.audio.playing or not self.audio_in.empty()):
                    # Mint is done (or its answer was only a "hmm"): the follow-up window starts.
                    # Talk that was not for Mint opens nothing.
                    self._window.finished(time.monotonic())
                self._heard = self._said = ""
                self._turn_open = False
                self._turn_levels = []
                self._reset_verdict()
                if not self._suppress_turn and self.loop is not None:
                    # Autopilot: a request left half done carries on without "next" from the user.
                    self.loop.call_later(1.0, lambda said=finished_said, at=time.monotonic(): self.loop.create_task(
                        self._autopilot_tick(said, self._stop_epoch, at)))
                if self._silent_reply:
                    self._silent_reply = False
                    self._last_voice = time.monotonic()
                    self._state(self._idle_state())

        if response.tool_call is not None and self._halted:
            # "stop" came between two tool calls: the model's turn goes on, but nothing more is done in it.
            self._print(f"[stopped: refused {', '.join(f.name for f in response.tool_call.function_calls)}]")
            asyncio.create_task(self._answer_all(response.tool_call, "STOPPED: the user said stop. Do nothing more, "
                                                                     "don't retry: just say 'Stopped.' and wait."))
        elif response.tool_call is not None:
            self._tool_call_at = time.monotonic()
            if any(f.name not in self._QUIET_TOOLS for f in response.tool_call.function_calls):
                self._tools_unspoken = getattr(self, "_tools_unspoken", 0) + 1
            self._mute = False               # the model moved on to work: its words are about that
            # In the background, so this loop keeps reading the server while a
            # tool runs: the user's words (and "stop") are heard mid-task.
            task = asyncio.create_task(self._run_tools(response.tool_call, self._stop_epoch))
            task.add_done_callback(self._tool_task_done)
            self._tool_task = task

        cancelled = getattr(response, "tool_call_cancellation", None)
        if cancelled is not None and getattr(cancelled, "ids", None):
            # The server gave up on these calls (the user interrupted). Its
            # answer must not come back for them.
            ids = set(cancelled.ids)
            if self._batch and ids & {i for i, _ in self._batch}:
                # The user talked over it. What is running is not thrown away: it goes on in the
                # background and is reported when it ends (background.py); the calls after it in
                # this batch are not run. Only "stop" stops it.
                self._print("[server cancelled the running tool: it goes on in the background]")
                self._batch = []
                self._batch_cancelled = True
                detach = getattr(self, "_detach_now", None)
                if detach is not None:
                    detach.set()

    async def _on_heard(self, chunk: str) -> None:
        """A piece of the transcript of what the microphone heard."""
        from mint.app import control
        from mint.core import guard
        from mint.voice import hearing
        from mint.core import prefs
        now = time.monotonic()
        if time.monotonic() - self._last_stop > 2.5:
            self._halted = False      # the user is talking again (not the "stop" itself): tools may run
        gap = now - self._last_chunk_at
        self._last_chunk_at = now
        if self._dropping and gap <= 1.0:
            # More of an utterance that was not for Mint: not kept, not answered - but a
            # "stop" or a yes/no for the guard said right after it still counts.
            if guard.current() is not None:
                guard.heard(chunk)
            if control.is_stop(chunk, prefs.get("stop_words")):
                await self.stop_everything("voice")
            return
        self._dropping = False
        if self._turn_open and (not self._voice_turn or (gap > 1.0 and (
                self._verdict == "ignore" or (self._verdict == "act"
                                              and getattr(self, "_model_active_at", 0.0) > now - gap)))):
            # New speech in a turn already under way (Mint is working or answering, or the
            # turn was typed): judged on its own.
            self._new_segment()
        if not self._turn_open:
            self._heard = self._said = ""
            self.ui.new_exchange()
            self._turn_open = True
            self._stop_armed = True
            self._begin_user_turn()
            new_turn = True
            self._turn_heard_at = now                 # an answer after this counts (see _voice_answer_watch)
            self._after_wake = bool(self._woke_at and now - self._woke_at < 30)
            if self._woke_at and time.monotonic() - self._woke_at < 30:
                # The first words after the wake word: a scrap of "Hey Mint"
                # the phrase cut missed ("payment"). The transcript can come
                # many seconds after the wake, once the sentence is done.
                self._woke_at = 0.0
                chunk, dropped = hearing.strip_wake(chunk)
                if dropped:
                    log.info("dropped a wake-phrase scrap from the transcript")
        else:
            new_turn = False
        chunk = hearing.apply(chunk)          # mishearings the user has corrected
        self._heard += chunk
        said = self._heard[self._seg:]        # the speech being judged (the whole turn, or new speech in it)
        if guard.current() is not None and guard.heard(said):
            self._guard_said = True           # "yes" / "no" to the guard's open question
        if getattr(self, "_model_active_at", 0.0) >= self._turn_began and self._verdict_pending():
            self._start_addressee_check()     # the model answered before these words came in
        if _foreign(self._heard):
            new_turn = False          # neither English nor Hindi: not shown
        try:
            from mint.app import live
            live.heard(chunk, new_turn=new_turn)
        except ImportError:
            pass
        if not _foreign(self._heard) and chunk.strip():
            self.ui.user_said(chunk)
            self._instant_poke()
        self._last_voice = time.monotonic()
        if self._stop_armed and control.is_stop(said, prefs.get("stop_words")):
            self._stop_armed = False
            await self.stop_everything("voice")

    async def compact(self, source: str = "button") -> str:
        """Like /compact: a Flash model summarises the conversation, and a new
        live session starts from that summary instead of the full back-and-forth.
        The context gets small again; what happened is kept - the user's own
        rules and requests word for word (compaction.py)."""
        summary = await self._compaction_summary(source)
        if summary.startswith(("Could not summarise", "There is nothing")):
            return summary + " The session was left as it was."
        await self.new_session(source, carry=summary)
        return summary

    async def _compaction_summary(self, source: str) -> str:
        """The sectioned summary of this conversation (history.jsonl since the last new session or
        compaction), updating the previous one; the plan under way goes in as it is."""
        from mint.app import compaction
        from mint.app import tasks
        from mint.knowledge.conversation import HISTORY
        since = max(float(getattr(self, "_fresh_at", 0.0) or 0.0), _STARTED)
        items = await asyncio.to_thread(compaction.entries, HISTORY, since)
        if not items:
            return "There is nothing to compact yet."
        plan = tasks.outline(self.task) if self.task else ""
        self._print(f"[compacting ({source}): {len(items)} lines{' + the last summary' if _carry_over else ''}]")
        try:
            return await asyncio.to_thread(compaction.summarize, items, _carry_over, plan)
        except Exception as error:
            log.warning("compaction failed: %s", str(error)[:160])
            return f"Could not summarise right now: {str(error)[:120]}"

    def _watch_context(self, meta, steps: int = 1) -> None:
        """Every turn's Live usage numbers: near the sliding window's trigger (where the server would
        silently drop the oldest turns, the user's first instructions with them), compact at the next
        quiet moment instead (pref auto_compact)."""
        from mint.app import compaction
        from mint.core import prefs
        tokens = compaction.context_tokens(meta) // max(1, steps)
        if not tokens:
            return
        self._context_tokens = tokens
        seen = self.__dict__.setdefault("_context_seen", [])
        seen.append(tokens)
        del seen[:-8]
        if (not compaction.due(tokens) or getattr(self, "_auto_compacting", False)
                or time.monotonic() < getattr(self, "_auto_compact_after", 0.0) or not prefs.get("auto_compact")):
            return
        self._auto_compacting = True
        log.info("context at %d tokens (%.0f%% of the window's trigger): compacting at the next quiet moment",
                 tokens, 100 * tokens / compaction.TRIGGER_TOKENS)
        self._print(f"[context {tokens} tokens{_by_modality(meta)}; last turns {', '.join(f'{n // 1000}k' for n in seen)}: "
                    "compacting at the next quiet moment]")
        asyncio.create_task(self._auto_compact())

    def _lost_words(self) -> str:
        """What the user said just before the connection dropped, if nothing had come back for it yet (no reply,
        no tool): the new connection never heard it, so it is sent again rather than the user asked to repeat."""
        spoken = " ".join((self._heard or "").split())
        spoke_at = getattr(self, "_spoke_at", 0.0)
        if (spoken and not (self._said or "").strip() and not self.asleep and time.monotonic() - spoke_at < 20
                and getattr(self, "_model_active_at", 0.0) < spoke_at):
            return spoken
        return ""

    def _go_away(self, going) -> None:
        """Google ends every Live connection after a while (about an hour; "1011 Deadline expired") and warns
        first. Reconnect before that, at a quiet moment, with the resumption handle: the conversation goes on
        and nothing the user says is lost in the drop (6 Oct: a user's question died with the connection)."""
        left = _seconds(getattr(going, "time_left", None))
        if getattr(self, "_go_away_task", None) is not None and not self._go_away_task.done():
            return
        self._print(f"[Google will end this connection in {left:.0f} s: reconnecting at a quiet moment]")
        self._go_away_task = asyncio.create_task(self._reconnect_before(left, self.session))

    async def _reconnect_before(self, left: float, session) -> None:
        from mint.app import compaction
        try:
            deadline = time.monotonic() + max(0.0, left - 3.0)
            await self._quiet_for(1.5, deadline)              # a quiet moment, or the last safe second
            while compaction.busy_reason(self) and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            if self.session is not session or session is None:
                return                                     # it dropped or was replaced meanwhile
            log.info("reconnecting before Google's go-away (handle kept: %s)", bool(self._resume_handle))
            self._restarting = True                        # _backoff: reconnect at once, quietly
            await session.close()
        except Exception:
            log.debug("reconnecting before the go-away failed", exc_info=True)

    async def _quiet_for(self, seconds: float, deadline: float) -> bool:
        """True once nothing has happened for `seconds` (compaction.busy_reason); False at the deadline."""
        from mint.app import compaction
        calm = None
        while time.monotonic() < deadline:
            if compaction.busy_reason(self):
                calm = None
            else:
                calm = calm or time.monotonic()
                if time.monotonic() - calm >= seconds:
                    return True
            await asyncio.sleep(0.5)
        return False

    async def _auto_compact(self) -> None:
        """Compact on our own, at a quiet moment: Mint not speaking, the user not talking, no tool or
        plan step running. Background jobs and the plan are outside the Live session and go on."""
        from mint.app import compaction
        from mint.knowledge.conversation import memory
        try:
            deadline = time.monotonic() + 15 * 60
            while await self._quiet_for(compaction.QUIET_FOR, deadline):
                await asyncio.to_thread(memory.checkpoint, 20)     # now, so new_session has nothing left to wait for
                summary = await self._compaction_summary("auto")
                if summary.startswith(("Could not summarise", "There is nothing")):
                    self._print(f"[auto compaction skipped: {summary[:120]}]")
                    self._auto_compact_after = time.monotonic() + 300
                    return
                if compaction.busy_reason(self):
                    continue                     # something started meanwhile: wait, and summarise again
                log.info("auto compaction at %d tokens", getattr(self, "_context_tokens", 0))
                await self.new_session("auto", carry=summary)
                return
            log.info("no quiet moment for an automatic compaction in 15 min; trying again after a later turn")
        except Exception:
            log.exception("automatic compaction failed")
            self._auto_compact_after = time.monotonic() + 300
        finally:
            self._auto_compacting = False

    async def new_session(self, source: str = "button", carry: str = "") -> None:
        """Start over: fold this conversation into long-term memory, forget the
        live context (no resumption handle), clear the chat, reconnect fresh -
        or, with `carry`, from a compacted summary."""
        global _carry_over
        from mint.knowledge.conversation import memory, HISTORY
        self._print(f"[{'compact' if carry else 'new session'} ({source})]")
        self._flush_playback()
        if not carry:                    # a compaction keeps the plan under way (its steps are in the summary)
            if self.task:
                from mint.app import tasks
                tasks.pause(self.task, "a new conversation was started")
            self.task = None
            self.ui.progress(0, 0)
        self._recent_calls = {}
        await asyncio.to_thread(memory.checkpoint, 20)       # the memory checkpoint before a fresh start
        self._resume_handle = None
        _carry_over = carry
        self._fresh_at = time.time()
        self._context_tokens = 0
        try:
            import json as _json
            with open(HISTORY, "a") as f:          # later compactions start from here
                f.write(_json.dumps({"t": time.strftime("%Y-%m-%d %H:%M"), "role": "marker",
                                     "text": "compacted" if carry else "new session"}) + "\n")
        except OSError:
            pass
        if carry and source == "auto":
            self.ui.chat_note("compacted automatically · your earlier instructions are kept")
        elif carry:
            self.ui.chat_reset("compacted · the session continues from this summary")
            self.ui.chat_card(carry)
        else:
            self.ui.chat_reset("new session · earlier talk is kept in Mint's memory")
        session = self.session
        if session is not None:
            self._restarting = True
            try:
                await session.close()
            except Exception:
                log.debug("closing the session for a new one failed", exc_info=True)

    async def _compact_when_quiet(self, summary: str) -> None:
        await asyncio.sleep(1.5)
        for _ in range(40):
            if not self.audio.playing and not self._busy and self.audio_in.empty():
                break
            await asyncio.sleep(0.5)
        await self.new_session("asked", carry=summary)

    async def _new_session_when_quiet(self) -> None:
        """Asked by voice: let Mint say its one sentence first."""
        await asyncio.sleep(1.5)
        for _ in range(40):
            if not self.audio.playing and not self._busy and self.audio_in.empty():
                break
            await asyncio.sleep(0.5)
        await self.new_session("asked")

    # Tools that need no words of their own (the orb's faces, plan bookkeeping): no rescue for them.
    _QUIET_TOOLS = {"express", "move_orb", "clear_marks", "step_done", "react"}
    MAX_RESCUES = 2
    _LEAK = re.compile(r"""(\bname\s*:\s*[a-z_]{3,}\s*\}|["']name["']\s*:\s*["']?[a-z_]|\buse_tool\s*\(|"""
                       r"""\bdefault_api\.|\btool_code\b|\bfunction_?call\b|\{\s*["']?(args|arguments)["']?\s*:)""", re.I)

    def _leak_check(self, chunk: str) -> bool:
        """The model 'said' a tool call (seen 10 Oct 17:43:51: Mint spoke ",name:find_files}" and did nothing).
        From the first sign of it the rest of the turn is cut: not played, not shown, not remembered; the turn's
        end asks for the real call (_silence_watch)."""
        if getattr(self, "_leaked", False):
            return True
        if not self._LEAK.search(self._said[-80:] + chunk):
            return False
        self._leaked = True
        self._flush_playback()
        self._said = ""
        self._print(f"[the model wrote a tool call as words ({chunk.strip()[:60]}): cut]")
        return True

    async def _silence_watch(self, epoch: int, since: float, wait: float, leaked: bool = False) -> None:
        """The model ran tools for the user's request and then went quiet - no words, no next call: ask it to
        answer (twice at most per request), so a request never just ends in silence. Waits `wait` seconds of
        nothing from the model, the user or a tool first; anything from them in that time means it is going on."""
        from mint.app import control
        from mint.app import live
        end = since + wait
        while time.monotonic() < end + 20:
            # Quiet counts from the last tool's end too: the model needs a few seconds after a big result (18:26:23,
            # a rescue 3 s after find_tools crossed the model's own next call).
            end = max(since, getattr(self, "_tools_at", 0.0) or 0.0) + wait
            await asyncio.sleep(0.25)
            if epoch != self._stop_epoch or control.stopped() or self.session is None:
                return
            if max(getattr(self, "_model_active_at", 0.0), getattr(self, "_nudged_at", 0.0)) > since + 0.05 or \
                    self._heard.strip() or self._busy or \
                    (self._tool_task is not None and not self._tool_task.done()):
                return
            if self.audio.playing or not self.audio_in.empty():
                continue
            if time.monotonic() >= end:
                break
        else:
            return
        if not leaked and getattr(self, "_tools_unspoken", 0) <= 0:
            return
        request = " ".join((live.request() or "").split())[:300]
        rescues = getattr(self, "_rescues", 0)
        if rescues >= self.MAX_RESCUES:
            self._print("[the model stayed quiet after its tools twice: telling the user]")
            self._tools_unspoken = 0
            self.ui.assistant_said("I lost track of that one - ask me again and I'll take another run at it.")
            self._window.finished(time.monotonic())
            return
        self._rescues = rescues + 1
        self._print("[the model went quiet after its tools: asking it to answer]"
                    if not leaked else "[asking the model to make the call it wrote as words]")
        note = ("(Mint note - not the user: your last reply came out as tool-call text, which did nothing and the "
                "user didn't hear. Make the real tool call now; don't mention this.)" if leaked else
                "(Mint note - not the user: you ran tools for the user's request and then said nothing, so they are "
                f"still waiting. The request: \"{request}\". Answer them now in one or two short sentences from the "
                "results you got: what you found or did. If it isn't finished, carry on with the next step. If "
                "something failed or found nothing, say so and try another way (another query, folder or tool) - "
                "never go quiet.)")
        self._nudge_turn = True
        self._nudged_at = time.monotonic()
        self._turn_open = True
        self._window.handling()
        try:
            await self.session.send_realtime_input(text=note)
        except Exception:
            log.debug("rescue note not sent", exc_info=True)
            self._turn_open = False

    def _tool_task_done(self, task: asyncio.Task) -> None:
        """A background batch must never die quietly: log what went wrong."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("tool batch crashed", exc_info=error)
            self._print(f"[tool batch crashed: {error!r}]")

    async def _autopilot_tick(self, said: str, epoch: int, ended_at: float = 0.0) -> None:
        """After a turn: if the request is only partly done, tell the model to carry on (autopilot.py)."""
        from mint.app import autopilot
        from mint.app import control
        # Only after 2 s with nothing from the model: it often goes straight on with its next tool call a
        # moment after a turn ends (in testing notes crossed quick express calls that a 0.25 s poll never
        # saw running). Any model output after this turn ended means it is still going - its own turn end
        # checks again. Never talk over the user, Mint's own voice or a running tool.
        for _ in range(80):
            if epoch != self._stop_epoch or control.stopped() or self.session is None or self.paused:
                return
            active = getattr(self, "_model_active_at", 0.0)
            if active > ended_at + 0.05 or self._turn_open or self._busy or self._heard.strip() or \
                    (self._tool_task is not None and not self._tool_task.done()):
                return
            if not self.audio.playing and self.audio_in.empty() and time.monotonic() - max(active, ended_at) >= 2.0:
                break
            await asyncio.sleep(0.25)
        else:
            return
        step = ""
        if self.task:
            from mint.app import tasks
            current = tasks.current(self.task)
            if current is not None:
                step = f"step {current['n']} of {tasks.progress(self.task)[1]} - {current['text']}"
        asking = False
        try:
            from mint.agents.runtime import hub
            asking = any(r.status == "asking" for r in hub.runs.values())
        except Exception:
            pass
        note = autopilot.decide(said, step, asking)
        if not note:
            # Only after a turn that ended with Mint's reply: a turn that ends in a tool call is
            # followed by the model's answer to the tool, a few seconds later.
            if epoch == self._stop_epoch and said.strip():
                # Finished: quiet, nothing running, nothing to carry on. bench/reliability waits for this line.
                self._print("[done]")
                from mint.app import telegram
                telegram.on_event("done", {})
            return
        if self.session is None or epoch != self._stop_epoch:
            return
        self._print(f"[autopilot: carrying on - {step or 'the rest of the request'}]")
        self._nudge_turn = True
        self._nudged_at = time.monotonic()
        self._turn_open = True
        self._window.handling()                # still working on the user's request: keep listening
        try:
            await self.session.send_realtime_input(text=note)
        except Exception:
            log.debug("autopilot note not sent", exc_info=True)
            self._turn_open = False

    def _permission_on(self, kind: str) -> None:
        """permit.py's watcher (any thread): the permission Mint asked for is on - carry on with the request."""
        from mint.core import permissions
        title = permissions.title(kind)
        self._print(f"[permission: {title} is on]")
        if self.loop is None or self.session is None or self.paused or self.asleep:
            return
        epoch = self._stop_epoch
        note = (f"[{title} is on now - the user switched it on. Carry on with what they asked, from where it "
                "stopped, now. Say one short line first, like 'Thanks, carrying on.']")

        async def carry_on():
            if epoch != self._stop_epoch or self.session is None:
                return
            self._nudge_turn = True
            self._turn_open = True
            self._window.handling()
            try:
                await self.session.send_realtime_input(text=note)
            except Exception:
                log.debug("permission note not sent", exc_info=True)
                self._turn_open = False
        asyncio.run_coroutine_threadsafe(carry_on(), self.loop)

    async def stop_everything(self, source: str) -> None:
        """The user said stop: cut off speech, the running tool, Desktop Voice's
        command and the task plan, and tell the model plainly."""
        now = time.monotonic()
        if now - self._last_stop < 1.5:
            return
        self._last_stop = now
        self._stop_epoch += 1
        self._halted = True
        self._unanswered = ""                 # a stopped request is not sent again after a reconnect
        self._asked_at = 0.0                  # nor waited on: its silence is not the model being slow
        from mint.app import autopilot
        from mint.app import control
        from mint.tools import desktop
        autopilot.stop()
        control.stop()                   # no more clicks or keys from any thread
        self._held, self._held_words = [], []
        self._flush_playback()
        desktop.cancel_running()
        pending = list(self._batch)
        finished = dict(self._batch_results)
        if self._tool_task is not None and not self._tool_task.done():
            self._tool_task.cancel()
        self._batch, self._batch_results = [], {}
        self._busy = False
        from mint.app import background
        still_running = background.on_stop()
        # A lesson on screen ends, and a demonstration being watched is wrapped up. A meeting
        # recording goes on: "stop" is said in meetings all the time.
        try:
            from mint.knowledge import teach
            from mint.ui import tutor
            if tutor.active():
                tutor.stop()
            if teach.recording():
                threading.Thread(target=teach.stop, daemon=True, name="teach-stop").start()
        except Exception:
            log.exception("stopping the lesson or the demonstration failed")
        had_task = self.task is not None
        if self.task:
            from mint.app import tasks
            tasks.pause(self.task, f"the user said stop ({source})")
        self.task = None
        self.ui.progress(0, 0)
        self.ui.stopped()
        self._print(f"[STOP ({source})" + (f": cancelled {', '.join(n for _, n in pending)}" if pending else "") + "]")
        from mint.app import telegram
        telegram.on_event("stop", {"source": source})
        from mint.knowledge.conversation import memory
        memory.add("tool", "The user said stop; everything in progress was stopped.")
        if pending and self.session is not None:
            note = ("STOPPED: the user said stop. Everything was cut off and nothing more will "
                    "be done. Do not continue, retry or resume the task" +
                    (" (the task plan was cancelled)" if had_task else "") +
                    ". Just say 'Stopped.' and wait." +
                    (f" ({still_running} They were NOT stopped - if the user meant them too, stop them with "
                     "stop_agent.)" if still_running else ""))
            responses = [types.FunctionResponse(
                id=i, name=n, response={"result": (finished[i] + " -- then: " + note) if i in finished else note})
                for i, n in pending]
            try:
                await self.session.send_tool_response(function_responses=responses)
            except Exception:
                log.debug("stop response failed", exc_info=True)
        self._state(self._idle_state())

    # --- was that meant for Mint? -------------------------------------------------

    # Said on their own, these need no answer. Noises always; acknowledgements
    # ("okay", "thanks", "acha") unless Mint has just asked something - then
    # they are the answer. In the logs "Okay." got "Is there anything else I
    # can help you with?", "Great, Boss!", "I'll keep you posted!".
    _NOISES = {"hmm", "hm", "hmmm", "hmmmm", "mm", "mmm", "mmmm", "mhm", "mhmm", "uh", "uhh", "um", "umm",
               "ah", "ahh", "er", "erm", "eh", "oh", "ooh", "huh-uh", "uh-huh"}
    _ACKS = {"okay", "ok", "okey", "k", "alright", "right", "yeah", "yep", "yup", "yes", "sure", "cool", "nice",
             "great", "fine", "good", "perfect", "awesome", "thanks", "thank", "you", "got", "it", "acha", "achha",
             "accha", "haan", "han", "ha", "theek", "thik", "hai", "ji", "so", "and", "well"}
    _ASKS = listening.ASKS

    def _filler_turn(self) -> bool:
        """The user's words so far are only a sound or an acknowledgement that
        needs no reply. Decided once per turn, on the model's first audio (or
        as soon as the words arrive, within the first second of it)."""
        if self._filler_checked or self._typed_turn:
            return False
        words = re.findall(r"[a-z]+(?:-[a-z]+)?", self._heard.lower())
        if not words:
            return False                      # not transcribed yet: look again on the next chunk
        self._filler_checked = True
        if len(words) > 4:
            return False
        if all(w in self._NOISES for w in words):
            return True
        if all(w in self._NOISES or w in self._ACKS for w in words):
            return not self._ASKS.search(self._last_said or "")
        return False

    def _begin_user_turn(self) -> None:
        """A new utterance from the user (already voice-verified if the lock is on)."""
        from mint.core import prefs
        self._suppress_turn = False
        self._goodbye_done = False
        self._filler_checked = False
        self._filler_hit = False
        self._typed_turn = False
        self._reset_verdict()
        self._voice_turn = True
        self._kind, self._next_kind = self._next_kind, "follow"
        if self.asleep and self._kind != "asked":
            self._kind = "follow"             # words that came after the window closed: no wake behind them
        if self.meet is not None:
            self._kind = "call"               # a call with Mint: everything said in it is for Mint (listening.quick)
        if not prefs.get("addressee_check"):
            self._decide("act", "rule")       # Settings: "Ignore talk meant for others" is off

    def _new_segment(self) -> None:
        """New speech in a turn already under way - while Mint works, answers, or carries out a
        typed request. It gets its own verdict; if it was not for Mint, Mint's spoken reaction is
        dropped (the work in progress carries on: it was the user's request)."""
        self._reset_verdict()
        self._voice_turn = True
        self._mid_turn = True
        self._seg = len(self._heard)
        self._stop_armed = True
        self._filler_checked = True
        self._kind, self._next_kind = self._next_kind, "follow"
        if self.meet is not None:
            self._kind = "call"
        from mint.core import prefs
        if not prefs.get("addressee_check"):
            self._decide("act", "rule")

    def _reset_verdict(self) -> None:
        self._verdict = None
        self._verdict_ready = asyncio.Event()
        self._check_task = None
        self._held, self._held_words = [], []
        self._seg = 0
        self._mid_turn = False
        self._mute = False
        self._dropping = False
        self._guard_said = False
        self._voice_turn = False
        self._turn_began = time.monotonic()

    def _verdict_pending(self) -> bool:
        return self._voice_turn and self._verdict is None

    async def _await_verdict(self, timeout: float) -> None:
        """Wait for the verdict on the user's words; too slow, and listening.fallback decides."""
        ready = self._verdict_ready
        if ready is None or not self._verdict_pending():
            return
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        if self._verdict_pending() and ready is self._verdict_ready:
            self._decide(listening.fallback(self._kind, self._heard[self._seg:], self._last_said,
                                            self._working_on_request()), "too slow")

    def _special(self, text: str) -> bool:
        """Words that always get through: stop, a goodbye, a yes/no to the guard, an instant
        command, and anything said while the user is showing Mint a skill or taking a lesson."""
        from mint.app import control
        from mint.core import prefs
        if self._guard_said or _is_goodbye(text) or control.is_stop(text, prefs.get("stop_words")):
            return True
        try:
            from mint.app import instant
            if instant.enabled() and instant.match(text) is not None:
                return True
        except Exception:
            log.debug("instant match failed", exc_info=True)
        try:
            from mint.knowledge import teach
            from mint.ui import tutor
            return bool(teach.recording() or tutor.active())
        except Exception:
            return False

    def _play_reply(self, data: bytes) -> None:
        """Mint's voice, to the speaker (or, silent, just the state)."""
        self._last_voice = time.monotonic()
        if not self.voice_on and self.meet is None:
            # Silent: the words still arrive as a transcript and are shown.
            if not self._silent_reply:
                self._silent_reply = True
                self._state("speaking")
            return
        self.audio_in.put_nowait(data)
        if self.half_duplex and not self.text_mode:
            # No echo cancellation: an open mic would hear Mint and it
            # would interrupt itself. `--barge-in` turns this off.
            self.audio.muted = True
        self._state("speaking")

    def _decide(self, verdict: str, how: str = "", took: float = 0.0, text: str | None = None) -> None:
        """Act on the user's words, or not. "act": what Mint said meanwhile plays, tools may run,
        the window stays open. "ignore": nothing is said or done for them (a whole turn is
        suppressed; new speech mid-turn only silences Mint's reaction), the window is not
        extended."""
        if not self._verdict_pending():
            return
        self._verdict = verdict
        if self._verdict_ready is not None:
            self._verdict_ready.set()
        text = " ".join((text if text is not None else self._heard[self._seg:]).split())
        self._window.judged(time.monotonic(), verdict in ("act", "wait"))
        held, words = self._held, self._held_words
        self._held, self._held_words = [], []
        if verdict == "act":
            if how and how != "rule":
                log.info("for me (%s, %.2fs): %s", how, took, text[:80])
            if not (self._suppress_turn or self._hush or self._mute):
                for chunk in held:
                    self._play_reply(chunk)
                for word in words:
                    self._said += word
                    self.ui.assistant_said(word)
            return
        if verdict == "wait":
            # Unfinished words ("I want to do", "ma'am"): nothing is said or done, and the window stays open for
            # the rest - the model sees them with what follows, as one request.
            self._window.judged(time.monotonic(), True)
        if self._mid_turn:
            self._mute = True
            self._dropping = True
            if verdict != "wait":
                self._heard = self._heard[:self._seg]
        else:
            self._suppress_turn = True
        self._flush_playback()
        if verdict == "wait":
            self._print(f"[waiting for the rest: {text[:80]}]")
        else:
            self._print(f"[not for me ({how}{f', {took:.1f}s' if took else ''}): {text[:80]}]")
        if not self._busy:
            self._state(self._idle_state())

    def _check_goodbye(self) -> None:
        """"Bye", said by the user, sends Mint to sleep - on the Mac, at once.
        Relying on the model missed it: "bye Mint" was transcribed "by mint",
        "by means", "by a mint", and it kept answering "I'm still here!"."""
        if self.asleep or self._goodbye_done or not _is_goodbye(self._heard):
            return
        self._goodbye_done = True
        self._print(f"[goodbye: {' '.join(self._heard.split())}]")
        # Let its own goodbye play - the first ~2 s of it - then nothing more:
        # it used to add "Goodbye!" and "I've already stopped listening. Feel
        # free to reach out..." after stop_listening answered.
        if _SLEEP_ONLY.search(self._heard):
            self._hush = True                  # asked to sleep: nothing said at all
            self._farewell_bytes = -1
            self._flush_playback()
        elif self._farewell_bytes < 0 and not self._hush:
            self._farewell_bytes = 0
        if self.hands_free:
            self.go_to_sleep("you said bye")

    def _start_addressee_check(self) -> None:
        """The model is answering: the user's words are in by now, so decide whether they were
        meant for Mint (listening.quick; Jev when the rules cannot tell)."""
        self._check_goodbye()
        said = self._heard[self._seg:]
        if self._verdict_pending() and _foreign(said):
            # Neither English nor Hindi (Latin or Devanagari script): not the user.
            self._decide("ignore", "not English or Hindi")
            return
        if not self._verdict_pending() or self._check_task is not None or not said.strip():
            return
        from mint.core import prefs
        verdict = listening.quick(said, self._kind, self._last_said, prefs.name(), self._special(said))
        if verdict is not None:
            self._decide(verdict, "rule")
            return
        self._check_task = asyncio.create_task(
            self._check_addressee(said, list(self._turn_levels), self._kind, self._verdict_ready))
        self._turn_levels = []

    async def _check_addressee(self, heard: str, levels: list[float], kind: str, ready) -> None:
        from mint.core import prefs
        from mint.voice import voicelock
        text = " ".join(heard.split())
        voice = "normal"
        usual = voicelock.lock.level
        if usual and levels:
            ratio = float(np.median(levels)) / usual
            if ratio < 0.45:
                voice = "much quieter than usual - maybe turned away from the microphone"
            elif ratio > 1.8:
                voice = "louder than usual"
        started = time.monotonic()
        try:
            pick = await asyncio.wait_for(asyncio.to_thread(
                listening.ask_jev, text, kind, self._last_said, voice, prefs.name()), 3.5)
        except (asyncio.TimeoutError, Exception):
            pick = None
        took = time.monotonic() - started
        if ready is not self._verdict_ready or not self._verdict_pending():
            return                        # a newer turn, or decided meanwhile
        working = self._working_on_request()
        if pick is None:
            self._decide(listening.fallback(kind, text, self._last_said, working), "no Jev", took, text)
            return
        option, probability, confidence = pick
        log.info("addressee %s p=%.2f c=%.2f in %.2fs (%s): %s", option, probability, confidence, took, kind, text)
        self._decide(listening.judge(kind, option, probability, self._last_said, text, working),
                     f"{option or 'none'} {probability:.2f}{' working' if working else ''}", took, text)

    def _job_open(self) -> bool:
        """Mid-job, for a model or key switch: working on the request and not already answered since the last tool
        (10 Oct 18:26: the answer was given, then a key switch "carried on" and did the whole search again)."""
        if not self._working_on_request():
            return False
        tool_task = getattr(self, "_tool_task", None)
        if getattr(self, "_busy", False) or (tool_task is not None and not tool_task.done()):
            return True
        try:
            from mint.app import tasks
            task = getattr(self, "task", None)
            if task and tasks.remaining(task):
                return True
        except Exception:
            pass
        return getattr(self, "_answered_at", 0.0) <= (getattr(self, "_tools_at", 0.0) or 0.0)

    def _working_on_request(self) -> bool:
        """Mint is in the middle of what the user asked: a plan with steps left, or a tool in the last 45 s."""
        tool_task = getattr(self, "_tool_task", None)
        if getattr(self, "_busy", False) or (tool_task is not None and not tool_task.done()):
            return True
        if time.monotonic() - (getattr(self, "_tools_at", 0.0) or 0.0) < 45:
            return True
        try:
            from mint.app import tasks
            task = getattr(self, "task", None)
            return bool(task) and bool(tasks.remaining(task)) and \
                time.time() - float(task.get("updated") or task.get("created") or 0) < 300
        except Exception:
            return False

    def _flush_playback(self) -> None:
        while not self.audio_in.empty():
            self.audio_in.get_nowait()
        if self.meet is not None:
            self.meet.clear()
        # The voice-processing player schedules audio ahead; stop it too.
        flush = getattr(self.audio, "flush", None)
        if flush is not None:
            flush()

    def _done_speaking(self) -> None:
        """Playback drained: reopen the mic and reset the idle clock."""
        if self.paused:
            self._audio_rest_later()
        self.audio.muted = False              # (whatever the mode is now: a rebuild may have changed it)
        self._last_voice = time.monotonic()
        if not self._turn_open:
            self._window.finished(self._last_voice)   # Mint is done: the follow-up window starts
        if self._wake is not None:
            # Mint's own voice may have leaked into the wake model's window.
            self._wake.reset()
        self._state(self._idle_state())

    # --- tools -----------------------------------------------------------------

    async def _run_tools(self, tool_call, epoch: int | None = None) -> None:
        """Run a batch - one batch at a time - and ALWAYS answer every call.

        If anything goes wrong the model still gets a result for each call: a
        batch that died without answering left Gemini waiting forever, silent
        (seen in testing after opening ZCode)."""
        async with self._tool_lock:
            if epoch is not None and epoch != self._stop_epoch:
                # Queued before the user said stop.
                await self._answer_all(tool_call, "STOPPED: the user said stop before this ran. Do nothing more.")
                return
            try:
                await self._run_batch(tool_call)
                if self.loop is not None and (epoch is None or epoch == self._stop_epoch):
                    self.loop.create_task(self._silence_watch(self._stop_epoch, time.monotonic(), 8.0))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.exception("tool batch failed")
                self._print(f"[tool batch failed: {error!r}]")
                self._busy = False
                self._batch = []
                await self._answer_all(tool_call, f"The tool failed with an internal error ({str(error)[:160]}); "
                                                  "nothing more was done. Tell the user briefly.")

    async def _answer_all(self, tool_call, text: str) -> None:
        if self.session is None:
            return
        try:
            await self.session.send_tool_response(function_responses=[
                types.FunctionResponse(id=f.id, name=f.name, response={"result": text})
                for f in tool_call.function_calls])
        except Exception:
            log.debug("could not answer the tool calls", exc_info=True)

    # Tools whose repeat within seconds is a duplicate, not a new request.
    _IDEMPOTENT = {"open_app", "open_url", "open_folder", "open_chrome", "open_slack", "switch_to",
                   "plan_task", "list_open", "list_accounts", "get_status", "frontmost_app", "list_windows"}

    async def _run_batch(self, tool_call) -> None:
        responses, images = [], []
        if self._verdict_pending():
            # Nothing is done on the screen until it is clear the words were
            # meant for Mint (about 0.4 s, when the rules cannot tell).
            self._start_addressee_check()
            await self._await_verdict(4.5)
        if (self._suppress_turn or self._hush) and self.session is not None:
            await self.session.send_tool_response(function_responses=[
                types.FunctionResponse(id=f.id, name=f.name, response={"result": (
                    "NOT RUN: the user said goodbye. Do nothing and say nothing." if self._hush else
                    "NOT RUN: the user was talking to someone else, not to you. Do nothing and "
                    "say nothing.")}) for f in tool_call.function_calls if f.name != "stop_listening"]
                or [types.FunctionResponse(id=f.id, name=f.name, response={"result": "Asleep. Say nothing."})
                    for f in tool_call.function_calls])
            return
        from mint.app import control
        control.resume()                 # a new request: input is allowed again
        self._busy = True
        self._heard_during_work = False
        self._batch = [(f.id, f.name) for f in tool_call.function_calls]
        self._batch_results = {}
        opened = False
        try:
            self._batch_cancelled = False
            for function in tool_call.function_calls:
                if control.stopped() or self._batch_cancelled:
                    return
                name, args = function.name, dict(function.args or {})
                # use_tool(name, args) is the hidden tool itself from here on: the guard, the phone gate, the
                # trace, undo and the screen lease all see the real name and arguments (tool_diet.py).
                from mint.tools import diet as tool_diet
                name, args, unusable = tool_diet.unwrap(name, args)
                if unusable:
                    result, image = unusable, None
                elif name in _OPENERS and opened:
                    # Each open brings a new tab or window to the front, and the
                    # next read or type acts on whatever is in front. In testing
                    # the model opened Gmail and then a new doc in one batch, so
                    # its "read the inbox" read the empty doc. One open per turn.
                    result, image = ("NOT RUN: open one thing at a time. Finish reading or "
                                     "working in what you just opened, then open this."), None
                else:
                    key = (name, repr(sorted(args.items())))
                    seen = self._recent_calls.get(key)
                    # A message typed and sent: never send the same words twice. Gemini
                    # 3.8 sent the ChatGPT prompt again 4 s after it went (24 Sep).
                    sends = self.__dict__.setdefault("_recent_sends", {})
                    words = " ".join(str(args.get("text", "")).split()).lower()[:160]
                    send_key = words if name in ("ui_act", "type_text") and args.get("press_return") and words \
                        and _messaging_front() else None
                    sent = sends.get(send_key) if send_key else None
                    # The same words typed twice doubles them in the field ("BotFatherBotFather"): seen 9 Oct, when
                    # Return was refused and the model typed the search again.
                    typed_map = self.__dict__.setdefault("_recent_typed", {})
                    typed_key = words if name == "type_text" and words else None
                    typed = typed_map.get(typed_key) if typed_key else None
                    if typed and time.monotonic() - typed < 45:
                        result, image = (f"ALREADY TYPED: '{str(args.get('text', ''))[:60]}' went in "
                                         f"{int(time.monotonic() - typed)}s ago and is still there - typing it again "
                                         "would double it. Go on from there: click the result or button you need by "
                                         "its name (click_text), or look first."), None
                    elif name in self._IDEMPOTENT and seen and time.monotonic() - seen[0] < 10:
                        result, image = (f"ALREADY DONE a moment ago, not repeated: {seen[1][:300]} "
                                         "Continue from there."), None
                    elif sent and time.monotonic() - sent[0] < 120 and not any(
                            w in sent[1] for w in ("FAILED", "WARNING", "Not confirmed", "not verified", "REFUSED",
                                                   "NOT RUN", "PERMISSION", "Cannot")):
                        result, image = (f"NOT SENT AGAIN: exactly this was already sent "
                                         f"{int(time.monotonic() - sent[0])}s ago ({sent[1][:160]}). Never send a "
                                         "message twice - wait for the reply with wait_until_done."), None
                    else:
                        if name in _OPENERS:
                            opened = True
                        result, image = await self._run_detachable(name, args)
                        from mint.core import permit
                        lacking = permit.from_result(result)      # a tool ran into a missing permission: ask now
                        if lacking:
                            result, image = permit.request(lacking, name, resume=self._permission_on), None
                        elif name in permit.NEEDS_ACCESSIBILITY | {"look", "read_window", "open_app"} and \
                                await asyncio.to_thread(permit.system_box_open):
                            result += permit.BOX_NOTE
                        result += _ladder(self, name, result)
                        if name == "click_at" and result.startswith(("NOT CLICKED", "FAILED", "UNVERIFIED")):
                            track = self.__dict__.setdefault("_click_misses", {"asked": None, "n": 0})
                            if track["asked"] != getattr(self, "_asked_at", 0.0):
                                track.update(asked=getattr(self, "_asked_at", 0.0), n=0)
                            track["n"] += 1
                        self._recent_calls[key] = (time.monotonic(), result)
                        if typed_key and result.startswith(("CONFIRMED", "Typed", "UNVERIFIABLE", "PARTIAL", "STILL RUNNING")):
                            typed_map[typed_key] = time.monotonic()
                        elif name in ("click_text", "click_at", "ui_act") and \
                                not result.startswith(("FAILED", "REFUSED", "NOT RUN", "NOT CLICKED", "UNVERIFIED")):
                            # A click may have moved to another field: the same words can go there. Not a key press -
                            # ⌘K brought back the same search box with the words still in it (10 Oct).
                            typed_map.clear()
                        if send_key:
                            sends[send_key] = (time.monotonic(), result)
                result = tool_diet.fit(name, result)        # a huge result: head + tail, the rest in a file
                self._print(f"[{name}] {result[:160]}")
                _trace(name if name == function.name else f"{name} (use_tool)", args, result)
                from mint.app import telegram
                telegram.on_event("tool_end", {"name": name, "args": args, "result": result})
                from mint.knowledge.conversation import memory
                memory.add("tool", f"{name}({_describe(name, args)}) -> {result[:240]}")
                self._last_voice = time.monotonic()
                responses.append(types.FunctionResponse(
                    id=function.id, name=function.name, response={"result": result}))
                self._batch_results[function.id] = result
                from mint.app import autopilot
                autopilot.note(name, args, result)
                if image is not None:
                    images.append(image)
        except asyncio.CancelledError:
            return                       # stopped: stop_everything answers for the batch
        finally:
            self._busy = False
            self._tools_at = time.monotonic()
        if control.stopped() or not self._batch:
            return
        self._batch = []

        if self.session is None:
            return
        # Screenshots go first, as video frames. The deprecated `media` field
        # crashed the session in testing ("media_chunks is deprecated"), and the
        # model starts answering as soon as it has the tool response, so the
        # image has to be there already.
        for image in images:
            await self.session.send_realtime_input(
                video=types.Blob(data=base64.b64decode(image["data"]), mime_type=image["mime_type"]))
        await self.session.send_tool_response(function_responses=responses)

    async def _run_detachable(self, name: str, args: dict) -> tuple[str, dict | None]:
        """Run one call; if it runs long, or the user talks over it, it goes on in the background
        (background.py) and the model is answered at once - so the conversation is never stuck
        behind it and nothing the user started is thrown away."""
        from mint.app import background
        from mint.core import guard
        work = asyncio.ensure_future(self._run_one(name, args))
        self._detach_now = asyncio.Event()
        started = time.monotonic()
        try:
            stays = name in background.NEVER_DETACH
            while not work.done():
                waits = [work] if stays or self._detach_now.is_set() else \
                    [work, asyncio.ensure_future(self._detach_now.wait())]
                await asyncio.wait(waits, timeout=0.25, return_when=asyncio.FIRST_COMPLETED)
                if len(waits) > 1:
                    waits[1].cancel()
                if work.done():
                    break
                interrupted = self._detach_now.is_set()
                slow = time.monotonic() - started > background.DETACH_AFTER
                if (interrupted or slow) and not stays and guard.current() is None:
                    why = "interrupted" if interrupted else "it is taking a while"
                    note = background.detach(work, name, args, why)
                    self._print(f"[{name}: going on in the background ({why})]")
                    return note, None
        except asyncio.CancelledError:
            # Stopped (stop_everything) - the call goes too.
            work.cancel()
            raise
        return work.result()

    async def _run_one(self, name: str, args: dict) -> tuple[str, dict | None]:
        from mint.app import telegram
        refused = telegram.gate(name, args)     # a read-only request from the phone: no sends, deletes or purchases
        if not refused:
            from mint.app import live
            refused = telegram.send_guard(name, args, live.request() or "")    # no sending unless asked, ever
        if refused:
            return refused, None
        if name == "click_at":
            track = self.__dict__.get("_click_misses", {"asked": None, "n": 0})
            if track["asked"] == getattr(self, "_asked_at", 0.0) and track["n"] >= CLICK_AT_MISSES:
                # Seen 10 Oct: 20+ click_at guesses on the bare desktop in one request, each refused, none learned
                # from. After three, pointing is off until the next request: words on screen or the keyboard.
                return (f"NOT RUN: click_at missed {track['n']} times in this request, so it is off until the next "
                        "one. Use click_text with words from the look's list of words on screen (it gives each "
                        "word's place), ui_act, or the keyboard (a shortcut, arrows, Tab).", None)
        if name == "background_task":
            from mint.app import background
            from mint.app import live
            refused = background.keep_in_front(str(args.get("task", "")), live.request() or "")
            if refused:
                return refused, None
        from mint.core import permit
        lacking = permit.missing(name, args)         # needs a permission Mint lacks: ask for it now, not fail quietly
        if lacking:
            return permit.request(lacking, name, resume=self._permission_on), None
        from mint.core import guard
        refused = await guard.check(name, args)      # deletes, overwrites, risky commands: the user's yes first
        if refused:
            return refused, None
        telegram.on_event("tool_start", {"name": name, "args": args})
        if name == "set_preference":
            return self._set_preference(str(args.get("setting", "")), str(args.get("value", ""))), None

        if name == "chat_action":
            what = str(args.get("action", "")).lower()
            if what == "clear":
                self.ui.chat_clear()
                return "Cleared the chat window. Long-term memory is kept.", None
            if what in ("summarize", "summarise", "compact"):
                # Summarising compacts: once this answer is spoken, the session
                # restarts from the summary (see _compact_when_quiet).
                summary = await self._compaction_summary("asked")
                if summary.startswith(("Could not summarise", "There is nothing")):
                    return summary, None
                asyncio.create_task(self._compact_when_quiet(summary))
                return (f"Summary (the session will now be compacted to it - you keep this summary, the "
                        f"details are dropped):\n{summary}\nTell the user the gist in one or two sentences "
                        "and that the conversation was compacted."), None
            if what in ("new_session", "new", "restart"):
                asyncio.create_task(self._new_session_when_quiet())
                return ("A fresh session starts in a moment; this conversation is saved to memory first. "
                        "Say one short sentence, like 'Starting fresh.'"), None
            return f"Unknown chat action '{what}'. Use clear, summarize or new_session.", None

        if name == "show_chat":
            want = args.get("open", True)
            want = want if isinstance(want, bool) else str(want).lower() not in ("false", "no", "off", "0")
            self.ui.show_chat(want)
            if not want:
                return "Chat window closed.", None
            await asyncio.sleep(0.5)
            from mint.screen import ground
            from mint.core import prefs
            buttons = [c["label"] for c in await asyncio.to_thread(ground.own_controls)]
            # Mint's own windows are kept out of screenshots, so `look` cannot
            # see them - in testing the model looked, saw nothing, and gave up.
            return ("Chat window opened. " + (f"Its buttons: {', '.join(buttons)}. " if buttons else "") +
                    f"To press one, use ui_act with app '{prefs.name()}' and the button's name. Your own windows do "
                    "not appear in look screenshots."), None

        if name == "stop_listening":
            already = self.asleep
            self.go_to_sleep("asked to stop")
            self._hush = True                 # anything more it would say is after goodbye
            self._farewell_bytes = -1
            return ("Already asleep." if already else "Asleep.") + " Say nothing more - no goodbye, no summary.", None

        if name == "set_timer":
            return self._start_timer(float(args.get("minutes", 1)), args.get("label", "")), None

        if name == "plan_task":
            from mint.app import tasks
            steps = [str(s) for s in (args.get("steps") or []) if str(s).strip()]
            goal = " ".join(str(args.get("goal", "")).split())
            busy = self.task is not None and tasks.remaining(self.task) and \
                time.time() - float(self.task.get("updated") or self.task.get("started") or 0) < 300
            asked = getattr(self, "_plan_refused", ("", 0.0))
            if busy and goal and goal.lower() != str(self.task.get("goal", "")).lower() and \
                    not (asked[0] == goal.lower() and time.monotonic() - asked[1] < 90):
                # A second request while a plan is under way: it must not push the first one aside.
                self._plan_refused = (goal.lower(), time.monotonic())
                done, total = tasks.progress(self.task)
                return (f"NOT STARTED: you are in the middle of '{self.task['goal']}' (step {done + 1} of {total}). "
                        "Do this new request alongside it with background_task (write the whole job), then carry "
                        "on with the current plan. Only if the user wants to drop the current one and do this "
                        "instead, call plan_task again with the same goal."), None
            self.task, paused = tasks.start(str(args.get("goal", "")), steps)
            self._print(f"[task] {self.task['goal']}: " + " | ".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
            if steps:
                self.ui.progress(0, len(steps), f"Step 1/{len(steps)}: {steps[0][:60]}")
            paused_note = (f" (Paused the earlier task {tasks.brief(paused)} - it can be resumed later.)"
                           if paused else "")
            # The skill and memories for this request, before the first step is
            # taken: without them the plan was made first and the known route
            # arrived too late (ZCode, in testing).
            try:
                from mint.tools import extra as extra_tools
                pack = await asyncio.to_thread(extra_tools.plan_context, self.task["goal"])
            except Exception:
                pack = ""
            return (f"Task started with {len(steps)} steps. Do step 1 now: {steps[0] if steps else ''}. "
                    "Call step_done after each step." + paused_note) + pack, None

        if name == "step_done":
            return self._step_done(str(args.get("step", "")), str(args.get("result", "")),
                                   bool(args.get("failed", False))), None

        if name == "task":
            return self._task_tool(args or {}), None

        from mint.ui import activity
        on_screen = activity.kind(name) in {"click", "type", "scroll", "open", "web", "switch"}
        self._state("working" if on_screen else "thinking", activity.phrase(name, args))
        self.ui.activity_start(name, args)
        if self.ui.needs_screen(name):
            # The chat had the keyboard; it was just handed back to the user's
            # app. Let that land before clicking or typing there.
            await asyncio.sleep(0.35)
        from mint.app import instant
        already = instant.claim(name, args)     # done the instant the user stopped talking: not twice
        if already:
            result, image = already, None
        else:
            instant.model_ran(name)             # and the instant path will not run it after this
            from mint.app import background

            def waiting(what: str) -> None:
                self._print(f"[{name}: {what}]")
            async with background.hold(background.Screen.YOU, name, args, waiting):
                result, image = await tools.dispatch(name, args)
        self.ui.activity_end(name, not _looks_failed(result))
        return result + self._context_after(name), image

    def _instant_poke(self) -> None:
        """After each piece of transcript: if the whole sentence so far is an instant command
        and no more words come for a moment, do it now (instant.py)."""
        from mint.app import instant
        if not instant.enabled() or self.loop is None:
            return
        heard = self._heard
        if instant.match(heard) is None:
            return

        async def later():
            await asyncio.sleep(instant.QUIET)
            last = getattr(self, "_instant_last", ("", 0.0))
            if self._heard != heard or not self._turn_open or self._suppress_turn \
                    or (last[0] == heard and time.monotonic() - last[1] < 4):
                return
            found = instant.match(heard)
            if found is None or instant.model_just_ran(found[0]):
                return
            name, args, label = found
            self._instant_last = (heard, time.monotonic())
            if name == "google_meet":
                instant.ran(name, args, heard.strip())
                self._print(f"[instant] {label}")
                if args.get("action") == "end":
                    from mint.app import meet_call
                    self.meet_note(meet_call.END_NOTE)
                    threading.Thread(target=meet_call.end, name="meet-end", daemon=True).start()
                else:
                    self._meet_request(spoken=True)
                return
            try:
                args = await asyncio.to_thread(instant.resolve, name, args)
                if instant.model_just_ran(name):
                    return
                instant.ran(name, args, heard.strip())
                self._print(f"[instant] {label}")
                await tools.dispatch(name, args)
            except Exception:
                log.exception("instant %s failed", name)
        self.loop.create_task(later())

    def _set_preference(self, setting: str, value: str) -> str:
        from mint.core import prefs
        on = value.strip().lower() in {"on", "true", "yes", "enable", "enabled", "1", "show"}
        switches = {"spoken_replies": "voice", "microphone": "mic", "face": "face",
                    "effects": "cursor_effects", "word_animation": "word_animation",
                    "listen_while_working": "listen_while_working", "activity_timeline": "timeline",
                    "instant_commands": "instant_commands"}
        if setting == "storage_folder":
            from mint.core import config
            raw = value.strip()
            if raw.lower() in {"", "default", "reset", "documents"}:
                prefs.set("storage_folder", "")
                return f"Mint saves what it makes in {config.storage()} again (the default)."
            path = os.path.expanduser(raw if raw.startswith(("/", "~")) else "~/" + raw)
            parent = os.path.dirname(path.rstrip("/"))
            if not os.path.isdir(path) and not os.path.isdir(parent):
                return f"There is no folder {parent}. Ask the user which folder they mean."
            prefs.set("storage_folder", path)
            return (f"From now on Mint saves meetings, videos, documents, spreadsheets and agent work in {config.storage()} "
                    f"(one sub-folder each). Files saved before stay where they are.")
        if setting == "reply_language":
            language = value.strip()
            auto = language.lower() in {"auto", "automatic", "default", "same", "any", "whatever i speak"}
            prefs.set("reply_language", "auto" if auto else language[:40].title())
            return ("From now on you answer in whatever language the user speaks." if auto else
                    f"From now on you always speak and write in {prefs.get('reply_language')} (it applies after a "
                    "quick reconnect).")
        if setting == "screenshot_to":
            where = value.strip().lower()
            where = "clipboard" if "clip" in where else "file" if where in ("file", "files", "desktop", "disk") else "both"
            prefs.set("screenshot_to", where)
            return {"clipboard": "Screenshots now go only to the clipboard (and the clipboard history, numbered) - no "
                                 "files.",
                    "file": "Screenshots are now saved as files only, not put on the clipboard.",
                    "both": "Screenshots are saved as files and put on the clipboard."}[where]
        if setting == "activity_timeline" and value.strip().lower() in {"clear", "delete", "forget", "erase"}:
            from mint.knowledge import timeline
            return timeline.clear()
        if setting in switches:
            prefs.set(switches[setting], on)
            if setting == "spoken_replies":
                return ("Spoken replies are ON: you speak again." if on else
                        "Spoken replies are OFF: from now on the user reads your replies in the chat "
                        "bubble instead of hearing them. Keep replies short and readable.")
            if setting == "activity_timeline":
                return ("The activity timeline is ON: while Mint runs it notes which app, window and web page is "
                        "in front (text only, never screenshots or typing; not private windows or password "
                        "managers), kept on this Mac for 14 days. Tell the user that, and that they can ask what "
                        "they worked on or where their time went." if on else
                        "The activity timeline is OFF; nothing more is recorded (what was recorded stays until "
                        "the user asks to delete it: activity_timeline=clear).")
            if setting == "microphone" and not on:
                return ("Microphone is OFF: you cannot hear the user now; they can type with the "
                        "chat (Cmd-J or click the orb) or turn the mic back on from the orb.")
            return f"{setting.replace('_', ' ').capitalize()} is now {'on' if on else 'off'}."
        if setting == "theme":
            theme = value.strip().lower()
            if theme not in prefs.THEMES:
                return f"Unknown theme '{value}'. Themes: {', '.join(prefs.THEMES)}."
            prefs.set("theme", theme)
            return f"Theme is now {theme}."
        if setting == "position":
            where = value.strip().lower().replace(" ", "-").replace("centre", "center")
            if where not in prefs.POSITIONS:
                return f"Unknown position '{value}'. Positions: {', '.join(prefs.POSITIONS)}."
            prefs.set("position", where)
            return f"Moved to the {where.replace('-', ' ')}."
        return f"Unknown setting '{setting}'."

    def _show_progress(self) -> None:
        from mint.app import tasks
        if not self.task:
            self.ui.progress(0, 0)
            return
        done, total = tasks.progress(self.task)
        step = tasks.current(self.task)
        if step is not None:
            self.ui.progress(done, total, f"Step {step['n']}/{total}: {step['text'][:60]}")

    def _step_done(self, step: str, result: str, failed: bool) -> str:
        from mint.app import tasks
        if not self.task:
            waiting = tasks.open_tasks()
            return ("There is no task running." + (f" A paused one: {tasks.brief(waiting[0])} - if this step belongs "
                                                   "to it, call task action=resume first." if waiting else
                                                   " Start one with plan_task."))
        problem = tasks.mark(self.task, step, result, failed)
        if problem:
            return problem
        done, total = tasks.progress(self.task)
        self._print(f"[task] step {step} {'FAILED' if failed else 'done'} ({done}/{total}): {result[:100]}")
        nxt = tasks.current(self.task)
        if nxt is None:
            summary = "; ".join(f"{s['n']}. {s['result'] or s['status']}" for s in tasks.leaves(self.task))
            self.task = None
            self.ui.progress(total, total, "✓ task complete")
            self.ui.celebrate()
            if self.loop is not None:
                self.loop.call_later(4, lambda: self.ui.progress(0, 0))
            return f"All steps finished. Tell the user briefly what was done: {summary}"
        self._show_progress()
        return f"Recorded. Next is step {tasks.label(nxt)}."

    def _task_tool(self, args: dict) -> str:
        from mint.app import tasks
        action = str(args.get("action") or "list").lower()
        steps = [str(s) for s in (args.get("steps") or []) if str(s).strip()]
        if action == "list":
            rows = tasks.open_tasks()
            if not rows:
                return "No unfinished tasks."
            return ("Unfinished tasks:\n" + "\n".join(f"- {tasks.brief(r)}" for r in rows)
                    + "\nIf the user wants to carry on, call task action=resume now"
                    + (" (which = words from the goal)." if len(rows) > 1 else "."))
        if action == "resume":
            found = tasks.find(str(args.get("which") or ""))
            if found is None:
                return "No unfinished task to resume" + (f" matching '{args.get('which')}'" if args.get("which") else "") + "."
            if self.task and self.task["id"] != found["id"]:
                tasks.pause(self.task, "another task was resumed")
            self.task = found
            self.task["state"] = "active"
            tasks.save(self.task)
            self._show_progress()
            return tasks.resume_text(self.task)
        if action == "abandon":
            found = self.task if not args.get("which") else tasks.find(str(args["which"]))
            if found is None:
                return "No such task."
            found["state"] = "abandoned"
            tasks.save(found)
            if self.task and self.task["id"] == found["id"]:
                self.task = None
                self.ui.progress(0, 0)
            return f"Dropped the task '{found['goal']}'."
        if not self.task:
            return "There is no task running. Start one with plan_task (or task action=resume)."
        if action == "add_steps":
            result = tasks.add_steps(self.task, steps, str(args.get("under") or ""), str(args.get("after") or ""))
            self._show_progress()
            nxt = tasks.current(self.task)
            return result + (f" Current step: {tasks.label(nxt)}." if nxt else "")
        if action == "replan":
            if not steps:
                return "FAILED: give the new steps."
            result = tasks.replan(self.task, steps)
            self._show_progress()
            return result + f"\n{tasks.outline(self.task)}"
        if action == "note":
            return tasks.note(self.task, str(args.get("text") or ""))
        if action == "pause":
            tasks.pause(self.task, str(args.get("text") or ""))
            self.task = None
            self.ui.progress(0, 0)
            return "Paused. It is saved; say 'continue' to pick it up."
        return f"Unknown task action '{action}'."

    def _context_after(self, name: str) -> str:
        """What a person would glance at after acting: what is in front now,
        and - in a long task - which step comes next."""
        notes = []
        if name in _OPENERS or name in {"desktop", "switch_to", "click_text", "click_at",
                                         "type_text", "export_doc_pdf"}:
            try:
                import AppKit
                from mint.tools import workspace
                front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
                title = ""
                if front is not None and front.bundleIdentifier() == workspace.CHROME_ID:
                    windows = workspace.chrome_windows(bring_forward=False)
                    selected = [t["title"] for w in windows[:1] for t in w["tabs"] if t["selected"]]
                    title = f" - tab '{selected[0][:60]}'" if selected else ""
                notes.append(f"[Now in front: {front.localizedName() if front else 'nothing'}{title}]")
            except Exception:
                pass
        since = time.time() - 180
        if self.task:
            from mint.app import tasks
            step = tasks.current(self.task)
            if step is not None:
                done, total = tasks.progress(self.task)
                notes.append(f"[Task '{self.task['goal'][:60]}': {done}/{total} steps done. Current: step "
                             f"{tasks.label(step)}. Call step_done when it is finished.]")
                since = min(since, float(self.task.get("created") or since) - 60)
        # What the user said while this is under way, pinned to every result: seen 9 Oct, "don't search with @, the
        # chat is already there" was forgotten two tool calls later and Mint did the same thing again.
        said = [text for at, text in getattr(self, "_user_lines", []) if at >= since][-3:]
        if said:
            notes.append("[The user's words for this, newest last - they override your plan: "
                         + " | ".join(f"\u201c{t}\u201d" for t in said) + "]")
        return ("\n" + "\n".join(notes)) if notes else ""

    def _start_timer(self, minutes: float, label: str) -> str:
        seconds = max(1.0, minutes * 60)
        name = label.strip() or "timer"

        async def ring():
            await asyncio.sleep(seconds)
            skills.notify("Mint", f"{name.capitalize()} is done.")
            await self.inject_text(
                f"(System: the {name} timer for {_duration(seconds)} just finished. "
                f"Tell the user now, in one short sentence.)")

        task = asyncio.create_task(ring())
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)
        return f"Timer set: {name}, {_duration(seconds)}."

    # --- lifecycle -------------------------------------------------------------

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        Mint.live = self                  # found by others without a gc scan (extra_tools._live_mint)
        if not self.hands_free:
            self._converse(True)            # always listening (no wake word): a conversation from the start
        from mint.agents import runtime as agent_hub
        agent_hub.hub.attach(self)          # sub-agents report to this session
        from mint.tools import automations
        from mint.knowledge import memory as membank
        automations.start()                 # schedules and triggers (they report through the hub)
        from mint.knowledge import timeline
        timeline.start()                    # the activity timeline (does nothing unless it is on)
        from mint.tools import trackers
        trackers.start_service()            # "let me know when ..." trackers left running before a restart
        from mint.app import telegram
        telegram.start(self)                # Telegram remote control: idle until a bot token is set and it is on
        from mint.core import usage
        usage.install()                     # token counts per model for Settings ▸ Usage
        from mint.app import updater
        updater.start(can_install=self._can_unload)   # "Updated to vX" card, checks, idle install
        from mint.tools import music
        music.start_service()               # now-playing watcher + the mini player
        from mint.tools import clipboard as clip_tools
        clip_tools.start_watching()         # the clipboard history, from the start (it survives restarts)
        from mint.knowledge import teach

        def teaching(state, detail=""):
            labels = {"recording": "● Watching you - say “done” when finished", "auto_stopped": "Stopped watching",
                      "saved": "Learned it ✓", "cancelled": "Stopped watching", "failed": "Could not learn it"}
            self._print(f"[teach {state}] {detail}"[:200])
            if state in labels:
                self.ui.action(labels[state])
        teach.on_change(teaching)
        from mint.tools import meetings
        if hasattr(meetings, "resume_pending"):
            # A meeting cut off by a quit or crash still gets its transcript and notes.
            threading.Timer(20, meetings.resume_pending).start()
        membank.tidy_later()                # merge duplicate memories, drop past ones (once a day)
        from mint.knowledge import learner
        learner.curate_later()              # skills unused for a month go stale, old stale ones are archived
        from mint.knowledge import history_index
        history_index.ingest_later(10)      # the history search index catches up, in its own thread
        attempt = 0
        self._state("starting")
        if self.ear is not None and self.ear.reason != "wake":
            self._ever_awake = True           # opened on purpose, not by a wake word to be verified
        if os.environ.get("MINT_EAR_REASON", "wake") != "wake":
            self._ever_awake = True
        if self.ear is not None:
            self._ear_task = asyncio.create_task(self._ear_feed())   # sets _own_mic when the Ear has let go
        else:
            self._own_mic.set()
        self._unload_task = asyncio.create_task(self._unload_watch())
        self._date_task = asyncio.create_task(self._date_watch())
        if not os.environ.get(config.API_KEY_ENV):
            # A first run: the key is added in the welcome window (onboarding's Connect page) or Settings.
            self._print("[waiting for a Gemini API key - add it in the welcome window or Settings ▸ Models & agents]")
            self._state("offline", "Add a Gemini key")
            while not os.environ.get(config.API_KEY_ENV):
                await asyncio.sleep(1.0)
            self._print("[Gemini key added - connecting]")
        while True:
            try:
                from mint.voice import live_models
                chosen = live_models.choose(config.MODEL)
                if chosen != config.MODEL:
                    # The model in use is benched (stalls, slow answers, errors): the next good one.
                    self._print(f"[voice model: {live_models.label(chosen)} - "
                                f"{live_models.label(config.MODEL)} is resting]")
                    config.MODEL = chosen
                    self._resume_handle = None    # a resumption handle belongs to its own model
                settings = _live_config()
                live_models.tune(settings, config.MODEL)
                client = _client()            # the key can change between connections (gemini_keys.py)
                if self._resume_handle:
                    settings.session_resumption = types.SessionResumptionConfig(
                        handle=self._resume_handle)
                async with client.aio.live.connect(model=config.MODEL, config=settings) as session:
                    self.session = session
                    self._connected_at = time.monotonic()
                    self._session_day = dt.date.today()   # the date the instructions give
                    attempt = 0
                    typing = " Type to send text; Ctrl-C to stop." if sys.stdin and sys.stdin.isatty() else ""
                    if self.hands_free:
                        from mint.voice.wake import display_phrase
                        word = display_phrase() if type(self._wake).__name__ == "MintWake" else self._wake_word.replace("_", " ")
                        print(f'\nMint is running (connected to {config.MODEL}). '
                              f'Say "{word}".{typing}\n', flush=True)
                    else:
                        print(f"\nMint is listening (connected to {config.MODEL}).{typing}\n", flush=True)
                    self._state(self._idle_state())
                    self._unanswered = self._unanswered or self._lost_words()
                    self._heard = self._said = ""
                    if self._unanswered and self._unanswered not in self._pending_text:
                        # The last session dropped (1011) before answering: it never heard it. Send it again.
                        self._print(f"[sending again after the drop: {self._unanswered[:80]}]")
                        self._pending_text.append(self._unanswered)
                    self._unanswered = ""
                    if self._pending_text:
                        asyncio.create_task(self._send_pending_text())
                    elif getattr(self, "_carry_note", ""):
                        asyncio.create_task(self._send_carry_note())
                    from mint.agents.runtime import hub as _hub
                    if _hub._outbox:
                        # Agent reports and automations that arrived while connecting.
                        asyncio.create_task(_hub.flush())

                    async with asyncio.TaskGroup() as group:
                        group.create_task(self._receive())
                        group.create_task(self._send_audio())
                        group.create_task(self._play())
                        if not self.text_mode:
                            group.create_task(self._listen())
                        if self.hands_free:
                            group.create_task(self._idle_watch())
                        group.create_task(self._prefer_watch())
                        if not self.text_mode:
                            group.create_task(self._mic_share_watch())
                        group.create_task(self._read_terminal())
                        group.create_task(self._meet_feed())
            except asyncio.CancelledError:
                raise
            except BaseExceptionGroup as errors:
                if any(isinstance(e, asyncio.CancelledError) for e in errors.exceptions):
                    raise asyncio.CancelledError from None
                attempt += 1
                await self._backoff(errors.exceptions[0], attempt)
            except Exception as error:
                attempt += 1
                await self._backoff(error, attempt)

    async def _backoff(self, error: BaseException, attempt: int) -> None:
        self.session = None
        if self._restarting:
            # new_session() closed it on purpose: reconnect at once, fresh.
            self._restarting = False
            return
        message = str(error)
        why = _outage_caption(message)
        if attempt == 1 and _routine_drop(message):
            # Google's hourly end of a connection, or one blip: reconnect at once without alarming anyone.
            # Only a second failure in a row is an outage worth a caption and a spoken word.
            log.info("session ended (%s); reconnecting", _short_error(message))
            self._state("offline", "Reconnecting…")
            await asyncio.sleep(0.5)
            return
        self._state("offline", why)
        self._tell_outage(why)
        from mint.core import gemini_keys
        if gemini_keys.failed(_live_env, message, "live"):
            # Rate-limited or refused, and a second Gemini key is set: reconnect on it at once, same model.
            print(f"  [Gemini {gemini_keys.label(_live_env)} "
                  f"{'refused' if gemini_keys.classify(message) == 'key' else 'is rate-limited'}; "
                  f"switching to {gemini_keys.label(gemini_keys.live_key()[0])}]", flush=True)
            self._resume_handle = None         # a resumption handle may not carry across keys
            return
        if "suspended" in message or "API key" in message or "PERMISSION_DENIED" in message:
            # Retrying cannot fix a bad key; say so plainly and stop.
            print(f"\nGemini rejected the API key: {_short_error(message)}\n"
                  "Run ./set-key.sh with a working key.\n", file=sys.stderr)
            raise SystemExit(2)
        lowered = message.lower()
        from mint.voice import live_models
        if "quota" in lowered or "resource_exhausted" in lowered or "429" in lowered:
            live_models.strike(config.MODEL, "out of quota")
            nxt = live_models.choose(config.MODEL)
            if nxt != config.MODEL:
                print(f"  [{live_models.label(config.MODEL)} is out of quota; switching to "
                      f"{live_models.label(nxt)}]", flush=True)
                config.MODEL = nxt
                self._resume_handle = None     # a resumption handle belongs to the other model
                return
        # Seen twice in one day on gemini-3.8-live: every reconnect answered
        # "1011 Internal error" until Mint gave up and quit; the relaunch,
        # which has no resumption handle, connected at once. So: the second
        # failure drops the handle (a fresh session, with memory), the fourth
        # moves to the fallback model for a while, and Mint never quits over
        # a server-side problem - it keeps trying every 30 s.
        if attempt >= 2 and self._resume_handle:
            print("  [reconnect keeps failing; starting a fresh session - memory is kept]", flush=True)
            self._resume_handle = None
        transient = any(word in lowered for word in (
            "1011", "internal", "unavailable", "timed out", "handshake", "503", "500", "overloaded", "1006"))
        # 5 Oct: gemini-3.8-live answered every turn - even a bare config with no tools - with "1011 Internal
        # error" for minutes, and voice requests vanished. After one fresh-session retry, use the next model
        # (benched longer each time it happens; live_models).
        # "1008 ... session not found" is the resumption handle having expired, not the model refusing Mint: seen
        # 9 Oct, it set the main model aside for a week and every request after ran on the weaker fallback. Start
        # a fresh session (memory is kept) on the same model instead.
        stale_handle = "session not found" in lowered
        if stale_handle:
            self._resume_handle = None
        refused = not stale_handle and any(word in lowered for word in (
            "not found", "not supported", "1007", "invalid argument", "thinking level"))
        if refused and len(live_models.pool()) > 1:
            # This model won't take Mint's session at all (a new model found by live_models.discover, or one
            # Google retired): set aside for a week, and the next model.
            live_models.retire(config.MODEL, _short_error(message))
        elif attempt >= 2 and transient:
            live_models.strike(config.MODEL, _short_error(message))
        if attempt >= 2 and transient or refused:
            nxt = live_models.choose(config.MODEL)
            if nxt != config.MODEL:
                print(f"  [{live_models.label(config.MODEL)} is failing ({_short_error(message)}); using "
                      f"{live_models.label(nxt)} for now, back when it recovers]", flush=True)
                config.MODEL = nxt
                self._resume_handle = None
                return
        if attempt == 7 or (attempt > 6 and not transient):
            traceback.print_exception(error)
        if attempt > 6 and not transient:
            raise SystemExit(1)
        delay = min(2 ** attempt, 30)
        log.warning("session dropped (%s); reconnecting in %ss", _short_error(message), delay)
        await self._count_down(delay, why)

    def _tell_outage(self, why: str) -> None:
        """The voice service failed: say so in the caption - and once, out loud (the Mac's own voice, as
        Gemini's is gone), when the user spoke to Mint just now and is waiting for an answer."""
        try:
            self.ui.action(why)
        except Exception:
            log.debug("outage caption failed", exc_info=True)
        now = time.monotonic()
        waiting = now - getattr(self, "_spoke_at", 0.0) < 30 and now - getattr(self, "_outage_said", 0.0) > 120
        if waiting and self.voice_on and not self.text_mode and self.meet is None:
            self._outage_said = now
            try:
                import subprocess
                subprocess.Popen(["/usr/bin/say", why.split(" - ")[0] + ". Trying again."],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                log.debug("could not say the outage", exc_info=True)

    async def _count_down(self, delay: float, why: str) -> None:
        """Wait before reconnecting, with the time left in the caption instead of silence."""
        end = time.monotonic() + delay
        while (left := end - time.monotonic()) > 0:
            seconds = max(1, round(left))
            line = f"{why} - trying again in {seconds} second{'' if seconds == 1 else 's'}."
            try:
                self.ui.action(line)
            except Exception:
                log.debug("outage caption failed", exc_info=True)
            await asyncio.sleep(min(left, 5.0 if left > 10 else 1.0))
        try:
            self.ui.action("Reconnecting…")
        except Exception:
            log.debug("outage caption failed", exc_info=True)

    # --- Mint Ear: hand-over and unloading (mint/app/ear.py) ------------------------

    async def _play(self) -> None:
        await self._own_mic.wait()        # starting playback starts the engine too (see _listen)
        await self.audio.play(self.audio_in, on_idle=self._done_speaking, on_chunk=self._on_play)

    async def _listen(self) -> None:
        # After a wake from Mint Ear, our microphone starts only once the user
        # pauses: starting it (voice processing) resets the Ear's microphone, and
        # in testing a sentence cut there lost its end.
        await self._own_mic.wait()
        if self.paused:
            self._audio_rest_later(5.0)        # started with the mic off: the engine comes up, then rests
        await self.audio.listen(self._on_audio)

    async def _ear_feed(self) -> None:
        """Replay what Mint Ear heard - the last 3 s before the wake word, then
        live audio - through the normal asleep path, until the user pauses. The
        same wake word model, voice lock and phrase cut apply.

        Then STOP: the Ear lets go of the microphone and exits, and only then
        does our own start. Both use voice processing, and a second voice
        processing reader turns the first one's audio into digital silence."""
        import queue as _queue

        from mint.voice.wake import rms
        link = self.ear
        self._ear_feeding = True
        started = time.monotonic()
        wake = link.reason == "wake"
        replayed = not wake
        quiet = 0.0                           # seconds of quiet live audio in a row (audio time)
        received = {"pre": 0, "live": 0}
        stop_at = None
        dump = open(os.environ["MINT_EAR_DUMP"], "wb") if os.environ.get("MINT_EAR_DUMP") else None
        try:
            if not wake:
                link.send("STOP")             # nothing being said: take the microphone now
                stop_at = 0.0
            # Speech after the wake word must reach a session: wait for Gemini (~0.6 s).
            while self.session is None and time.monotonic() - started < 10:
                await asyncio.sleep(0.05)
            while True:
                try:
                    item = await asyncio.to_thread(link.chunks.get, True, 0.3)
                except _queue.Empty:
                    item = ("idle", b"")
                if item is None:
                    break                     # the Ear has let go
                kind, data = item
                if kind != "pre" and not replayed:
                    replayed = True
                    if self.asleep and not self._pending_wake and self._wake is not None:
                        # Our detector did not fire on the replay (it starts cold):
                        # the Ear's did, so take its word for where the phrase ended.
                        self._wake.phrase_end_lag = link.lag
                        if self._wake_is_user():
                            await self.wake_up("wake word")
                if kind in received:
                    received[kind] += len(data)
                    if dump is not None:
                        dump.write(data)
                for k in range(0, len(data), 3200):
                    piece = data[k:k + 3200]
                    if kind == "live":
                        quiet = 0.0 if rms(piece) > 0.012 else quiet + len(piece) / 32000
                    await self._on_audio(piece, from_ear=True)
                now = time.monotonic()
                if stop_at is None and replayed and (quiet >= 0.9 or now - started > 12):
                    link.send("STOP")         # the user paused
                    stop_at = now - started
                if stop_at is not None and now - started - stop_at > 1.5:
                    break                     # the Ear should be gone by now
        except Exception:
            log.exception("Mint Ear hand-over failed")
        finally:
            if stop_at is None:
                link.send("STOP")
            self._ear_feeding = False
            link.close()
            self.ear = None
            self._own_mic.set()               # our microphone starts now
            if dump is not None:
                dump.close()
            self._print(f"[ear hand-over ({link.reason}): {received['pre'] / 32000:.1f} s before the wake word + "
                        f"{received['live'] / 32000:.1f} s live; stop at {stop_at or 0:.1f} s; "
                        f"our microphone at {time.monotonic() - started:.1f} s]")

    def _can_unload(self) -> bool:
        from mint.agents.runtime import hub
        busy = (not self.asleep or self._busy or self._timers or self.enroller is not None
                or self._pending_text or self._pending_wake or self._ear_feeding
                or getattr(self, "_recalibrating", False)
                or any(run.active for run in hub.runs.values()) or hub._finished or hub._outbox)
        if busy:
            return False
        # A background screen watch (wait_until_done) reports back later; a Codex
        # run finished recently can be resumed ("make it darker") only while its
        # Run is in memory.
        try:
            from mint.tools import work as work_tools
            if work_tools.busy():
                return False
        except Exception:
            pass
        try:
            from mint.tools import automations
            if automations.keep_loaded():     # a folder/app watcher, or one due within minutes
                return False
            from mint.tools import meetings
            from mint.tools import screenrec
            from mint.knowledge import teach
            from mint.tools import trackers
            from mint.ui import tutor
            if tutor.keep_loaded() or teach.recording() or meetings.busy() or screenrec.busy() or trackers.busy():
                return False
        except Exception:
            pass
        now = time.time()
        if any(run.agent.get("runner") == "codex" and run.finished and now - run.finished < 1800
               for run in hub.runs.values()):
            return False
        hud = getattr(self.ui, "hud", None)
        if hud is not None and getattr(hud, "console_open", False):
            return False
        # Mid-trick (squashed, tilted, or away from its spot): the orb picture the
        # Ear shows would be wrong. Wait until it is home and still.
        try:
            from mint.ui.motion import motion
            if motion._legs or motion._away:
                return False
        except Exception:
            pass
        # Settings, Skills & Memory, training: a window the user is in.
        try:
            import AppKit
            if any(w.isVisible() and w.canBecomeKeyWindow() for w in AppKit.NSApp.windows()):
                return False
        except Exception:
            pass
        return True

    async def _date_watch(self) -> None:
        """The instructions carry the date the session started. After midnight "tomorrow" meant the wrong
        day (bench, a run that crossed midnight). Once the date changes and Mint is idle, reconnect - the
        conversation is kept - so the instructions carry the new date."""
        while True:
            await asyncio.sleep(60)
            day = getattr(self, "_session_day", None)
            if day is None or day == dt.date.today() or self.session is None:
                continue
            if self._busy or self._turn_open or (self._tool_task is not None and not self._tool_task.done()):
                continue
            self._session_day = dt.date.today()
            self._print("[a new day: reconnecting so the date is right]")
            from mint.tools import extra as extra_tools
            extra_tools.schedule_voice_reconnect(self)

    async def _unload_watch(self) -> None:
        """Asleep and idle long enough: unload, and let Mint Ear listen (~30 MB)."""
        from mint.app import ear
        from mint.core import prefs
        if not ear.managed() or self.text_mode:
            return
        while True:
            await asyncio.sleep(15)
            if prefs.get("notch_mode"):
                continue                     # the Ear's stand-in is the floating orb, hidden in notch mode
            minutes = prefs.get("unload_after_minutes")
            try:
                minutes = float(minutes)
            except (TypeError, ValueError):
                continue
            if minutes <= 0:
                continue
            # Started for a wake word that turned out not to be the user's: go back
            # soon. Opened on purpose (⌘J, the menu, the app itself): the full time -
            # in testing it unloaded 2 minutes after the user opened the console.
            limit = minutes * 60 if self._ever_awake else min(minutes * 60, 120)
            idle = time.monotonic() - max(self._last_active, self._last_voice)
            if idle < limit or not self._can_unload():
                continue
            self._print(f"[unloading after {idle / 60:.1f} min asleep - Mint Ear listens meanwhile]")
            await asyncio.to_thread(self._unload)

    def _unload(self) -> None:
        from mint.app import ear
        try:
            if not ear.snapshot_orb(self.ui):
                log.info("no orb picture; the Ear shows a plain orb")
        except Exception:
            log.exception("orb picture failed")
        self.close()
        logging.shutdown()
        os._exit(ear.UNLOADED)

    def close(self) -> None:
        for task in list(self._timers):
            task.cancel()
        self.audio.close()
        try:
            from mint.tools import cdp
            cdp.shutdown()                    # Mint's own web browser goes with it (the user's Chrome stays)
        except Exception:
            log.debug("web browser shutdown failed", exc_info=True)
        # Fold the rest of today's conversation into memory before quitting,
        # but never hold up quitting for long.
        from mint.knowledge.conversation import memory
        memory.close(timeout=8)


# --- helpers ---------------------------------------------------------------------

def _jobs_note() -> str:
    """Background jobs started earlier that are still running (they outlive a reconnect)."""
    try:
        from mint.app import background
        note = background.running_note()
    except Exception:
        return ""
    return (note + " They report when they end; do not start them again.") if note else ""


def _describe(name: str, args: dict) -> str:
    """A short, human caption for a tool call, shown in the HUD."""
    main = next((str(v) for k, v in args.items() if k in ("goal", "url", "name", "text", "title", "action", "direction", "key")), "")
    words = name.replace("_", " ")
    return f"{words} · {main[:70]}" if main else words


_GOODBYE = re.compile(
    r"^(?:(?:ok(?:ay)?|thanks?|thank you|chalo|acha|achha|theek hai|right|alright|cool|for now|now|so|"
    r"please|just|you can|mint)[,.!]?\s+)*"
    r"(?:bye(?:[- ]bye)?|by|bi|buy|good ?bye|good ?night|see you(?: later)?|(?:go (?:to )?)?sleep(?: now)?|"
    r"go to bed|so jao|alvida|that'?s all|that is all|that'?s it|nothing else|band karo|stop listening)"
    r"(?:[,.!]?\s+(?:mint|a mint|means|meant|then|now|for now|ji|bye|buddy|dost|please))*[\s.!?]*$",
    re.IGNORECASE)
# "Sleep" (not "bye"): no goodbye at all, just sleep (8 Oct: "for now sleep" was missed here, went to the model,
# and a fallback voice model answered "A system error occurred.").
_SLEEP_ONLY = re.compile(r"\b(sleep|so jao|go to bed|stop listening)\b", re.IGNORECASE)


def _is_goodbye(text: str) -> bool:
    """Short and nothing but a goodbye: "bye", "by mint", "ok bye Mint", "good night"."""
    text = " ".join(text.split())
    from mint.core import prefs
    name = prefs.name().lower()
    if name != "mint":
        # "bye Nova" works like "bye Mint".
        text = re.sub(rf"\b{re.escape(name)}\b", "mint", text, flags=re.IGNORECASE)
    return 0 < len(text.split()) <= 5 and bool(_GOODBYE.match(text))


CALL_APPS = [
    "us.zoom", "com.microsoft.teams", "com.apple.FaceTime", "com.apple.avconferenced", "com.cisco.webex",
    "Cisco-Systems.Spark", "com.tinyspeck.slackmacgap", "com.google.Chrome", "com.apple.Safari",
    "com.apple.WebKit", "org.mozilla.firefox", "com.brave.Browser", "company.thebrowser.Browser",
    "com.microsoft.edgemac", "com.operasoftware.Opera", "net.whatsapp.WhatsApp", "desktop.WhatsApp",
    "com.hnc.Discord", "com.skype.skype", "ru.keepcoder.Telegram", "com.loom.desktop",
    "com.obsproject.obs-studio", "com.apple.QuickTimePlayerX", "com.apple.VoiceMemos",
    "com.logmein.GoToMeeting", "com.ringcentral",
]


def _app_label(user: dict) -> str:
    """The app a capturing process belongs to, as the user knows it: Chrome's helper is "Google Chrome"
    (it used to say "helper is using the microphone")."""
    bundle = str(user.get("bundle") or "")
    for prefix in CALL_APPS:
        if bundle.lower().startswith(prefix.lower()):
            try:
                import AppKit
                url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(prefix)
                if url is not None:
                    return str(url.lastPathComponent()).removesuffix(".app")
            except Exception:
                pass
            break
    name = str(user.get("name") or "")
    return name if name and name.lower() not in ("helper", "plugin") else (bundle or "another app")


def _is_call_app(bundle: str, extra: list) -> bool:
    """Calls, meetings, browsers (Meet), recorders - not any app that keeps a
    mic open (the Claude app's helper captures permanently on this Mac, which
    would otherwise pause Mint forever)."""
    bundle = (bundle or "").lower()
    return bool(bundle) and any(bundle.startswith(prefix.lower()) for prefix in CALL_APPS + list(extra))


def _foreign(text: str) -> bool:
    """Mostly letters of a script other than Latin or Devanagari."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    other = sum(1 for c in letters if not (c.isascii() or "\u0900" <= c <= "\u097f" or "\u00c0" <= c <= "\u024f"))
    return other / len(letters) > 0.3


def _looks_failed(result: str) -> bool:
    lowered = result.lower()
    return any(mark in lowered for mark in ("could not", "cannot", "failed", "not allowed",
                                            "timed out", "nothing was done", "refused"))


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{int(round(seconds))} seconds"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:g} minutes".replace(".0 ", " ")
    return f"{minutes / 60:.1f} hours"


def _seconds(value) -> float:
    """A protobuf Duration, a timedelta or "50s" in seconds (60 when unknown)."""
    try:
        if value is None:
            return 60.0
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
        if hasattr(value, "seconds"):
            return float(value.seconds) + float(getattr(value, "nanos", 0) or 0) / 1e9
        return float(str(value).strip().rstrip("s"))
    except (TypeError, ValueError):
        return 60.0


SLOW_MEDIAN = 2.0      # seconds to the first sound or tool call, the median of the last 3 requests
SLOW_STALL = 3.0       # nothing at all back this long after a request: another model, at once
PROMISE_WAIT = 10.0    # "I'll start that" with the turn left in progress, and no tool call this long after


def _too_slow(recent: list[float]) -> str:
    """Why the model is too slow to keep ('' if it isn't)."""
    if len(recent) >= 3:
        last = sorted(recent[-3:])[1]
        if last > SLOW_MEDIAN:
            return f"answers took {last:.1f} s"
    if sum(1 for took in recent if took > SLOW_STALL) >= 2:
        return f"{sum(1 for took in recent if took > SLOW_STALL)} answers took over {SLOW_STALL:g} s"
    return ""


def _routine_drop(message: str) -> bool:
    """A connection that ended the ordinary way - Google's time limit, a going-away close, a network blip -
    rather than a refused key, quota or bad settings."""
    lowered = " ".join(str(message).split()).lower()
    if any(word in lowered for word in ("api key", "permission_denied", "quota", "resource_exhausted", "429",
                                        "1007", "invalid argument", "suspended")):
        return False
    return any(word in lowered for word in ("deadline expired", "1000", "1001", "going away", "1006", "1011",
                                            "1008", "aborted", "internal error", "connection reset", "closed"))


def _short_error(message: str) -> str:
    message = " ".join(message.split())
    return message[:90] + ("…" if len(message) > 90 else "")


# A dropped or refused Live session, in one plain line for the caption (most specific first).
_OUTAGES = (
    (("api key", "api_key", "permission_denied", "unauthenticated", "suspended", " 401", " 403"),
     "Gemini refused the API key - check it in Settings"),
    (("429", "quota", "resource_exhausted", "rate limit", "too many requests"),
     "Gemini's quota is used up for now"),
    (("getaddrinfo", "nodename", "name or service", "network is unreachable", "no route", "connection refused",
      "connection reset", "errno 8", "errno 51", "errno 61", "ssl", "cannot connect", "connecterror", "1006",
      "timed out", "timeout", "handshake"),
     "No connection to the voice service - check the internet"),
    (("1011", "internal", "unavailable", "overloaded", "503", "500", "502", "deadline"),
     "The voice service is down on Google's side"),
    (("1007", "invalid argument", "invalid_argument"),
     "The voice service refused Mint's settings"),
)


def _outage_caption(message: str) -> str:
    lowered = f" {' '.join(str(message).split()).lower()}"
    for words, caption in _OUTAGES:
        if any(word in lowered for word in words):
            return caption
    return "The voice service dropped"
