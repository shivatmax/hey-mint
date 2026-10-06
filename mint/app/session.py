"""The Gemini Live session: audio in, audio out, tool calls, wake and sleep.

Microphone routing is the heart of it:

    asleep  ->  mic frames go ONLY to the local wake word model. Nothing is sent.
    awake   ->  mic frames stream to Gemini Live.
    paused  ->  mic frames are dropped entirely, wake word included.
"""

from __future__ import annotations

import asyncio
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
    personal = custom.prompt_addendum()
    remembered = memory.summary()
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
                   + (f"\n\n{personal}" if personal else "")
                   + (f"\n\n{words}" if words else "")
                   + (f"\n\nWhat you remember from earlier conversations with this user "
                      f"(your own notes; use them, do not recite them):\n{remembered}" if remembered else "")
                   + (f"\n\n{jobs}" if (jobs := _jobs_note()) else "")
                   + (f"\n\n{compaction.framed(_carry_over)}" if _carry_over else ""))
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
                                    defer=_ear.defer_audio())
            echo_cancelled = self.audio.full_duplex
            self.audio.on_reconfigure = self._audio_reconfigured
            self._print(f"[audio: {self.audio.status}]")
        except Exception as error:
            log.warning("audio engine unavailable (%s); using half duplex", error)
            self.audio = Audio()
            echo_cancelled = False
        if half_duplex is None:
            half_duplex = not echo_cancelled
        self._mic_lent_to = ""                 # a call or meeting app has the mic
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

    def go_to_sleep(self, reason: str = "quiet") -> None:
        if self.asleep or not self.hands_free or self.meet is not None:
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

    def _audio_reconfigured(self, audio) -> None:
        """The engine was rebuilt (devices, echo mode, or it had stopped)."""
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
        """Step aside while a call or meeting app has the microphone.

        Two apps capturing at once - and Mint's echo canceller next to a call's
        own - is what made calls and meetings misbehave. Every 1.5 s this asks
        CoreAudio which apps are capturing (listeners on that do not fire on
        current macOS); when a call app is, Mint stops talking and lets go of
        the mic and speaker entirely, and takes them back 3 s after it stops."""
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
                if self._mic_lent_to:
                    self._take_mic_back()
                continue
            try:
                users = await asyncio.to_thread(audio_devices.mic_users)
            except Exception:
                continue
            callers = [u for u in users if _is_call_app(u["bundle"], prefs.get("share_mic_apps") or [])]
            if callers and not self._mic_lent_to:
                name = callers[0]["name"]
                self._mic_lent_to = name
                self._flush_playback()
                if self._gate is not None:
                    self._gate.cancel()
                await asyncio.to_thread(self.audio.suspend, name)
                self._print(f"[{name} is using the microphone - Mint steps aside until it's done]")
                self._offer_meeting_notes(name)
                self._state("paused", f"{name} is using the mic")
                free_since = 0.0
            elif not callers and self._mic_lent_to:
                free_since = free_since or time.monotonic()
                if time.monotonic() - free_since >= 3.0:
                    self._take_mic_back()
                    free_since = 0.0

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
        if self.meet is not None:
            return                                # on a Google Meet call, the call is the microphone (_on_meet_audio)
        if self._ear_feeding and not from_ear:
            # Mint Ear is still handing over what it heard while this app
            # started; our own microphone takes over once that has caught up.
            self._mic_alive = True
            return
        from mint.voice import dictation
        from mint.voice import wake_train
        if wake_train.capturing():
            wake_train.feed(pcm)                  # Settings ▸ Voice & wake word is recording a take
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
            if self._wake.heard(pcm) and self._wake_is_user():
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
                self._state("awake")
            if event.endswith("end"):
                self._voice_sent(time.monotonic())
        elif event == "end" and not self.asleep and getattr(self, "_users_words", False):
            self._users_words = False
            self._voice_sent(time.monotonic())        # the user's verified words just finished
        elif event.startswith("closed"):
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
        if getattr(self, "_voice_watch_task", None) is None or self._voice_watch_task.done():
            self._voice_watch_task = self.loop.create_task(self._voice_watch())

    async def _voice_watch(self, wait: float = 10.0) -> None:
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
        if self._server_at > spoke or self.session is None or self._busy or self.audio.playing:
            self._deaf_voice = 0
            return
        self._deaf_voice = getattr(self, "_deaf_voice", 0) + 1
        primary = config.MODEL
        if self._deaf_voice >= 1 and config.MODEL != config.FALLBACK_MODEL:
            config.MODEL = config.FALLBACK_MODEL
            self._primary_task = asyncio.create_task(self._return_to_primary(primary))
            self._print(f"[{primary} is not answering; using {config.FALLBACK_MODEL} for now]")
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

    def _wake_is_user(self) -> bool:
        """With the voice lock on, only the user's "Hey Mint" wakes Mint.

        Clear yes: wake now. Clear no: ignore. In between: hold the phrase and
        let the gate decide on the next second of speech ("Hey Mint, open
        Slack" gives it plenty); see the pending-wake branch in _on_audio."""
        if self._gate is None:
            return True
        from mint.voice import voicelock
        audio = voicelock.voiced(voicelock.to_float(self._preroll.peek()),
                                 floor=self._gate.speech_threshold())
        verdict, phrase, voice = voicelock.lock.wake_verdict(audio)
        if verdict == "yes":
            log.info("wake word voice check: phrase %.2f voice %.2f", phrase, voice)
            return True
        if verdict == "maybe":
            self._pending_wake = time.monotonic()
            held = self._preroll.drain()
            # The phrase stays in what the gate judges (it is voice evidence);
            # it is cut from what is sent once the voice is confirmed.
            self._wake_cut = wake_phrase_cut(held, getattr(self._wake, "phrase_end_lag", None))
            self._gate.begin_pending(held)
            self._print(f"[Hey Mint - checking the voice (phrase {phrase:.2f}, voice {voice:.2f})]")
            return False
        self._print(f"[Hey Mint in another voice - ignored] (phrase {phrase:.2f}, voice {voice:.2f})")
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

    async def _idle_watch(self) -> None:
        """Fall asleep once the listening window has passed (listening.Window): a few seconds after
        Mint is done, unless work is in progress, Mint is speaking, or the user's words are still
        being judged. Talk that was not for Mint does not keep it open."""
        while True:
            await asyncio.sleep(0.5)
            if self.asleep or self.paused or not self.hands_free:
                continue
            now = time.monotonic()
            playing = self.audio.playing or not self.audio_in.empty()
            if playing:
                self._last_voice = now
            # Words waiting for their verdict (bounded: a turn the model never answers ends too).
            deciding = bool(self._held) or (self._verdict_pending()
                                            and now - self._last_chunk_at < listening.JUDGE_WAIT)
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
        if (meta := getattr(response, "usage_metadata", None)) is not None:
            from mint.core import usage
            usage.live(config.MODEL, meta)
            self._watch_context(meta)        # near the sliding window: compact at a quiet moment
        if update := getattr(response, "session_resumption_update", None):
            if getattr(update, "resumable", False) and getattr(update, "new_handle", None):
                self._resume_handle = update.new_handle
            return
        self._last_active = self._server_at = time.monotonic()
        self._unanswered = ""

        if response.data or response.tool_call is not None or (
                response.server_content is not None and response.server_content.output_transcription):
            self._model_active_at = time.monotonic()      # for the autopilot's "quiet for 2 s"
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
                else:
                    self._said += server.output_transcription.text
                    self.ui.assistant_said(server.output_transcription.text)
                    if getattr(self, "_chat_reply", None) is not None:
                        self._chat_reply.append(server.output_transcription.text)

            if server.interrupted:
                self._flush_playback()

            if server.turn_complete:
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
                    if self._heard.strip():
                        self._print(f"you:    {' '.join(self._heard.split())}")
                        memory.add("user", self._heard)
                        from mint.knowledge import teach
                        if teach.recording():
                            teach.add_narration(self._heard)      # what the user says while showing Mint
                    if self._said.strip():
                        self._print(f"mint: {' '.join(self._said.split())}")
                        from mint.app import telegram
                        telegram.on_event("reply", {"text": " ".join(self._said.split())})
                        memory.add("mint", self._said)
                        self._last_said = self._said
                finished_said = "" if self._suppress_turn else self._said
                self._check_empty_done(finished_said)
                self._check_refused_job(finished_said)
                if (not self._suppress_turn or self._verdict == "act") and \
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

    def _watch_context(self, meta) -> None:
        """Every turn's Live usage numbers: near the sliding window's trigger (where the server would
        silently drop the oldest turns, the user's first instructions with them), compact at the next
        quiet moment instead (pref auto_compact)."""
        from mint.app import compaction
        from mint.core import prefs
        tokens = compaction.context_tokens(meta)
        if not tokens:
            return
        self._context_tokens = tokens
        if (not compaction.due(tokens) or getattr(self, "_auto_compacting", False)
                or time.monotonic() < getattr(self, "_auto_compact_after", 0.0) or not prefs.get("auto_compact")):
            return
        self._auto_compacting = True
        log.info("context at %d tokens (%.0f%% of the window's trigger): compacting at the next quiet moment",
                 tokens, 100 * tokens / compaction.TRIGGER_TOKENS)
        self._print(f"[context {tokens} tokens: compacting at the next quiet moment]")
        asyncio.create_task(self._auto_compact())

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
        self._turn_open = True
        self._window.handling()                # still working on the user's request: keep listening
        try:
            await self.session.send_realtime_input(text=note)
        except Exception:
            log.debug("autopilot note not sent", exc_info=True)
            self._turn_open = False

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
            self._decide(listening.fallback(self._kind, self._heard[self._seg:], self._last_said), "too slow")

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
        if self._farewell_bytes < 0 and not self._hush:
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
        if pick is None:
            self._decide(listening.fallback(kind, text, self._last_said), "no Jev", took, text)
            return
        option, probability, confidence = pick
        log.info("addressee %s p=%.2f c=%.2f in %.2fs (%s): %s", option, probability, confidence, took, kind, text)
        self._decide(listening.judge(kind, option, probability, self._last_said, text),
                     f"{option or 'none'} {probability:.2f}", took, text)

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
        if self.half_duplex:
            self.audio.muted = False
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
                    if name in self._IDEMPOTENT and seen and time.monotonic() - seen[0] < 10:
                        result, image = (f"ALREADY DONE a moment ago, not repeated: {seen[1][:300]} "
                                         "Continue from there."), None
                    elif sent and time.monotonic() - sent[0] < 120 and not any(
                            w in sent[1] for w in ("FAILED", "WARNING", "Not confirmed", "not verified")):
                        result, image = (f"NOT SENT AGAIN: exactly this was already sent "
                                         f"{int(time.monotonic() - sent[0])}s ago ({sent[1][:160]}). Never send a "
                                         "message twice - wait for the reply with wait_until_done."), None
                    else:
                        if name in _OPENERS:
                            opened = True
                        result, image = await self._run_detachable(name, args)
                        self._recent_calls[key] = (time.monotonic(), result)
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
        if self.task:
            from mint.app import tasks
            step = tasks.current(self.task)
            if step is not None:
                done, total = tasks.progress(self.task)
                notes.append(f"[Task '{self.task['goal'][:60]}': {done}/{total} steps done. Current: step "
                             f"{tasks.label(step)}. Call step_done when it is finished.]")
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
        while True:
            try:
                settings = _live_config()
                client = _client()            # the key can change between connections (gemini_keys.py)
                if self._resume_handle:
                    settings.session_resumption = types.SessionResumptionConfig(
                        handle=self._resume_handle)
                async with client.aio.live.connect(model=config.MODEL, config=settings) as session:
                    self.session = session
                    self._session_day = dt.date.today()   # the date the instructions give
                    attempt = 0
                    typing = " Type to send text; Ctrl-C to stop." if sys.stdin and sys.stdin.isatty() else ""
                    if self.hands_free:
                        word = self._wake_word.replace("_", " ")
                        print(f'\nMint is running (connected to {config.MODEL}). '
                              f'Say "{word}".{typing}\n', flush=True)
                    else:
                        print(f"\nMint is listening (connected to {config.MODEL}).{typing}\n", flush=True)
                    self._state(self._idle_state())
                    if self._unanswered and self._unanswered not in self._pending_text:
                        # The last session dropped (1011) before answering: it never heard it. Send it again.
                        self._print(f"[sending again after the drop: {self._unanswered[:80]}]")
                        self._pending_text.append(self._unanswered)
                    self._unanswered = ""
                    if self._pending_text:
                        asyncio.create_task(self._send_pending_text())
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
        self._state("offline", why)
        self._tell_outage(why)
        from mint.core import gemini_keys
        if gemini_keys.failed(_live_env, message, "live"):
            # Rate-limited or refused, and a second Gemini key is set: reconnect on it at once, same model.
            print(f"  [Gemini key {_live_env[-1] if _live_env.endswith('2') else '1'} "
                  f"{'refused' if gemini_keys.classify(message) == 'key' else 'is rate-limited'}; "
                  "switching to the other key]", flush=True)
            self._resume_handle = None         # a resumption handle may not carry across keys
            return
        if "suspended" in message or "API key" in message or "PERMISSION_DENIED" in message:
            # Retrying cannot fix a bad key; say so plainly and stop.
            print(f"\nGemini rejected the API key: {_short_error(message)}\n"
                  "Run ./set-key.sh with a working key.\n", file=sys.stderr)
            raise SystemExit(2)
        lowered = message.lower()
        if ("quota" in lowered or "resource_exhausted" in lowered) and config.MODEL != config.FALLBACK_MODEL:
            print(f"  [{config.MODEL} is out of quota; switching to {config.FALLBACK_MODEL}]", flush=True)
            config.MODEL = config.FALLBACK_MODEL
            self._resume_handle = None         # a resumption handle belongs to the other model
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
        # error" for minutes, and voice requests vanished. After one fresh-session retry, use the fallback.
        if attempt >= 2 and transient and config.MODEL != config.FALLBACK_MODEL:
            print(f"  [{config.MODEL} is failing ({_short_error(message)}); using {config.FALLBACK_MODEL} "
                  "for now, back when it recovers]", flush=True)
            primary, config.MODEL = config.MODEL, config.FALLBACK_MODEL
            self._resume_handle = None
            self._primary_task = asyncio.create_task(self._return_to_primary(primary))
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

    async def _return_to_primary(self, primary: str) -> None:
        """After an outage moved Mint to the fallback model: every ten minutes,
        while Mint is asleep (never mid-conversation), try the main model again.
        If it is still failing, _backoff moves back here on its own."""
        await asyncio.sleep(600)
        while config.MODEL == config.FALLBACK_MODEL:
            session = self.session
            if session is not None and self.asleep and not getattr(self, "_busy", False):
                print(f"  [trying {primary} again]", flush=True)
                config.MODEL = primary
                self._resume_handle = None
                self._restarting = True
                try:
                    await session.close()
                except Exception:
                    log.debug("closing the session to return to the main model failed", exc_info=True)
                return
            await asyncio.sleep(60)

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
    r"^(?:(?:ok(?:ay)?|thanks?|thank you|chalo|acha|achha|theek hai|right|alright|cool)[,.!]?\s+)*"
    r"(?:bye(?:[- ]bye)?|by|bi|buy|good ?bye|good ?night|see you(?: later)?|go to sleep|sleep now|"
    r"so jao|alvida|that'?s all|that is all|that'?s it|nothing else|band karo|stop listening)"
    r"(?:[,.!]?\s+(?:mint|a mint|means|meant|then|now|for now|ji|bye|buddy|dost))*[\s.!?]*$",
    re.IGNORECASE)


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
