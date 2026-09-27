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
import os
import sys
import time
import traceback

from google import genai
from google.genai import types

from mint.core import config
from mint.tools import everyday as skills
from mint.tools import registry as tools
from mint.voice.audio import Audio
from mint.voice.wake import phrase_cut as wake_phrase_cut

log = logging.getLogger("mint.app.session")


def _client() -> genai.Client:
    return genai.Client(
        http_options={"api_version": "v1beta"},
        api_key=os.environ[config.API_KEY_ENV],
    )


# Set by Mint.compact(): the compacted conversation the next session starts from.
_carry_over = ""


def _live_config() -> types.LiveConnectConfig:
    # The model has no clock. Giving it the start time lets it reason about
    # "tomorrow at nine"; get_status covers long sessions.
    local = dt.datetime.now().astimezone()
    now = local.strftime("%A %B %-d %Y, %-I:%M %p")
    offset = local.strftime("%z")
    zone = f"{local.tzname()} (UTC{offset[:3]}:{offset[3:]})"
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
                   + ("" if config.desktop_voice() else
                      "\n\nThe desktop tool is not installed on this Mac: wherever these "
                      "instructions mention it, use ui_act (or click_text) instead.")
                   + (f"\n\n{personal}" if personal else "")
                   + (f"\n\n{words}" if words else "")
                   + (f"\n\nWhat you remember from earlier conversations with this user "
                      f"(your own notes; use them, do not recite them):\n{remembered}" if remembered else "")
                   + (f"\n\nThis conversation was just compacted. Everything said so far, summarised "
                      f"(continue from it as if you remember it; do not read it out):\n{_carry_over}"
                      if _carry_over else ""))
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
        tools=_named(tools.tools(), name),
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


class _NoUI:
    """Stand-in when there is no menu bar or HUD."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Mint:
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
        self._expect_command = True     # the first thing said after waking is for Mint
        self._addr_candidate = False    # this turn should be checked: for Mint or not?
        self._addr_task: asyncio.Task | None = None
        self._suppress_turn = False     # the user was talking to someone else
        self._last_said = ""
        self._turn_levels: list[float] = []
        self._ignored_at = 0.0
        self._pending_text: list[str] = []   # typed while reconnecting; sent once connected
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
        self._goodbye_done = False

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
        self._woke_at = time.monotonic() if reason.startswith("wake word") else 0.0
        self._expect_command = True
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
        if self.asleep or not self.hands_free:
            return
        self.asleep = True
        self._last_active = time.monotonic()
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
        self._print("[paused]" if paused else "[resumed]")
        self.ui.set_paused(paused)
        if paused and self.loop is not None and self.session is not None:
            # The mic may have gone off mid-sentence: tell the server the speech
            # is over, or its next typed turn waits for it (see _close_user_audio).
            asyncio.run_coroutine_threadsafe(self._close_user_audio(), self.loop)

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
                self._state("paused", f"{name} is using the mic")
                free_since = 0.0
            elif not callers and self._mic_lent_to:
                free_since = free_since or time.monotonic()
                if time.monotonic() - free_since >= 3.0:
                    self._take_mic_back()
                    free_since = 0.0

    def _take_mic_back(self) -> None:
        name, self._mic_lent_to = self._mic_lent_to, ""
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
        if self._ear_feeding and not from_ear:
            # Mint Ear is still handing over what it heard while this app
            # started; our own microphone takes over once that has caught up.
            self._mic_alive = True
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
            if self.audio.playing and not self.half_duplex:
                self._flush_playback()
                self._print(f"[you interrupted - listening] (voice {self._gate.last_score:.2f})")
                self._state("awake")
        elif event.startswith("closed"):
            now = time.monotonic()
            if now - self._ignored_at > 4:
                self._print(f"[ignored another voice] (voice {self._gate.last_score:.2f})")
            self._ignored_at = now

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

    async def _idle_watch(self) -> None:
        """Fall asleep again after a quiet spell, unless work is in progress."""
        while True:
            await asyncio.sleep(0.5)
            if self.asleep or self.paused or not self.hands_free or self._busy:
                continue
            if self.audio.playing or not self.audio_in.empty():
                self._last_voice = time.monotonic()
                continue
            if time.monotonic() - self._last_voice > self.sleep_after:
                self.go_to_sleep(f"quiet for {self.sleep_after:.0f}s")

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

    async def inject_text(self, text: str) -> None:
        """Send a typed request, as if spoken. Used by the terminal, `mint --say`, and timers."""
        if not text.strip():
            return
        self._last_active = time.monotonic()
        self._ever_awake = True
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
            live.typed(text)
        except ImportError:
            pass
        self._turn_open = True
        self._suppress_turn = False
        self._addr_candidate = False
        self._print(f"[typed: {text[:120]}]")
        # Close any open stretch of microphone audio first: in testing, a typed
        # request sent while the mic was streaming room noise went unanswered -
        # the server was still waiting for that "speech" to end.
        if not self.asleep and not self.paused:
            await self._end_audio_stream()
        else:
            await self._close_user_audio()
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
        if update := getattr(response, "session_resumption_update", None):
            if getattr(update, "resumable", False) and getattr(update, "new_handle", None):
                self._resume_handle = update.new_handle
            return
        self._last_active = time.monotonic()

        if response.data or response.tool_call is not None or (
                response.server_content is not None and response.server_content.output_transcription):
            self._model_active_at = time.monotonic()      # for the autopilot's "quiet for 2 s"
            # The model is answering: the user's words are in by now, so this
            # is when to ask whether they were meant for Mint at all.
            self._start_addressee_check()

        if response.data and self._suppress_turn:
            pass                         # not for Mint: its answer is not played
        elif response.data:
            self._last_voice = time.monotonic()
            if not self.voice_on:
                # Silent: the words still arrive as a transcript and are shown.
                if not self._silent_reply:
                    self._silent_reply = True
                    self._state("speaking")
            else:
                self.audio_in.put_nowait(response.data)
                if self.half_duplex and not self.text_mode:
                    # No echo cancellation: an open mic would hear Mint and it
                    # would interrupt itself. `--barge-in` turns this off.
                    self.audio.muted = True
                self._state("speaking")

        server = response.server_content
        if server is not None:
            if server.input_transcription and server.input_transcription.text:
                from mint.voice import hearing
                chunk = server.input_transcription.text
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
                if _foreign(self._heard):
                    new_turn = False          # neither English nor Hindi: not shown
                try:
                    from mint.app import live
                    live.heard(chunk, new_turn=new_turn)
                except ImportError:
                    pass
                if not _foreign(self._heard) and chunk.strip():
                    self.ui.user_said(chunk)
                self._last_voice = time.monotonic()
                from mint.app import control
                from mint.core import prefs
                if self._stop_armed and control.is_stop(self._heard, prefs.get("stop_words")):
                    self._stop_armed = False
                    await self.stop_everything("voice")

            if server.output_transcription and server.output_transcription.text and not self._suppress_turn:
                self._said += server.output_transcription.text
                self.ui.assistant_said(server.output_transcription.text)

            if server.interrupted:
                self._flush_playback()

            if server.turn_complete:
                self._check_goodbye()
                from mint.knowledge.conversation import memory
                if self._suppress_turn:
                    # Someone else's conversation: not the user's history.
                    if self._heard.strip():
                        self._print(f"(not for me: {' '.join(self._heard.split())})")
                else:
                    if self._heard.strip():
                        self._print(f"you:    {' '.join(self._heard.split())}")
                        memory.add("user", self._heard)
                        self._expect_command = False
                    if self._said.strip():
                        self._print(f"mint: {' '.join(self._said.split())}")
                        memory.add("mint", self._said)
                        self._last_said = self._said
                finished_said = "" if self._suppress_turn else self._said
                self._heard = self._said = ""
                self._turn_open = False
                self._turn_levels = []
                if not self._suppress_turn and self.loop is not None:
                    # Autopilot: a request left half done carries on without "next" from the user.
                    self.loop.call_later(1.0, lambda said=finished_said, at=time.monotonic(): self.loop.create_task(
                        self._autopilot_tick(said, self._stop_epoch, at)))
                if self._silent_reply:
                    self._silent_reply = False
                    self._last_voice = time.monotonic()
                    self._state(self._idle_state())

        if response.tool_call is not None:
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
                self._print("[server cancelled the running tool]")
                from mint.app import control
                from mint.tools import desktop
                control.stop()
                desktop.cancel_running()
                if self._tool_task is not None and not self._tool_task.done():
                    self._tool_task.cancel()
                self._batch = []

    async def compact(self, source: str = "button") -> str:
        """Like /compact: a Flash model summarises the conversation, and a new
        live session starts from that summary instead of the full back-and-forth.
        The context gets small again; what happened is kept."""
        from mint.ui.chat import summarize_text
        transcript = (await asyncio.to_thread(self.ui.chat_transcript) or "") if self.ui else ""
        if not transcript.strip():
            transcript = self._history_transcript()
        if not transcript.strip():
            return "There is nothing to compact yet."
        self._print(f"[compacting ({source}): {len(transcript)} characters]")
        summary = await asyncio.to_thread(summarize_text, transcript)
        if summary.startswith("Could not summarise"):
            return summary + " The session was left as it was."
        await self.new_session(source, carry=summary)
        return summary

    def _history_transcript(self) -> str:
        """The conversation since the last clear/new session, from history.jsonl
        (for when there is no chat window)."""
        import json
        from mint.knowledge.conversation import HISTORY
        try:
            entries = [json.loads(line) for line in HISTORY.read_text().splitlines()[-200:] if line.strip()]
        except (OSError, ValueError):
            return ""
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].get("role") == "marker":
                entries = entries[i + 1:]
                break
        names = {"user": "User", "mint": "Mint", "tool": "Tool"}
        return "\n".join(f"{names.get(e.get('role'), e.get('role'))}: {e.get('text', '')}"
                         for e in entries if e.get("role") in names)

    async def new_session(self, source: str = "button", carry: str = "") -> None:
        """Start over: fold this conversation into long-term memory, forget the
        live context (no resumption handle), clear the chat, reconnect fresh -
        or, with `carry`, from a compacted summary."""
        global _carry_over
        from mint.knowledge.conversation import memory, HISTORY
        self._print(f"[{'compact' if carry else 'new session'} ({source})]")
        self._flush_playback()
        self.task = None
        self._recent_calls = {}
        self.ui.progress(0, 0)
        await asyncio.to_thread(memory.summarize_now)
        self._resume_handle = None
        _carry_over = carry
        try:
            import json as _json
            with open(HISTORY, "a") as f:          # later compactions start from here
                f.write(_json.dumps({"t": time.strftime("%Y-%m-%d %H:%M"), "role": "marker",
                                     "text": "compacted" if carry else "new session"}) + "\n")
        except OSError:
            pass
        if carry:
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
            steps = self.task["steps"]
            remaining = [i for i in range(1, len(steps) + 1) if i not in self.task["done"]]
            if remaining:
                step = f"step {remaining[0]} of {len(steps)} - {steps[remaining[0] - 1]}"
        asking = False
        try:
            from mint.agents.runtime import hub
            asking = any(r.status == "asking" for r in hub.runs.values())
        except Exception:
            pass
        note = autopilot.decide(said, step, asking)
        if not note or self.session is None or epoch != self._stop_epoch:
            return
        self._print(f"[autopilot: carrying on - {step or 'the rest of the request'}]")
        self._turn_open = True
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
        from mint.app import autopilot
        from mint.app import control
        from mint.tools import desktop
        autopilot.stop()
        control.stop()                   # no more clicks or keys from any thread
        self._flush_playback()
        desktop.cancel_running()
        pending = list(self._batch)
        finished = dict(self._batch_results)
        if self._tool_task is not None and not self._tool_task.done():
            self._tool_task.cancel()
        self._batch, self._batch_results = [], {}
        self._busy = False
        had_task = self.task is not None
        self.task = None
        self.ui.progress(0, 0)
        self.ui.stopped()
        self._print(f"[STOP ({source})" + (f": cancelled {', '.join(n for _, n in pending)}" if pending else "") + "]")
        from mint.knowledge.conversation import memory
        memory.add("tool", "The user said stop; everything in progress was stopped.")
        if pending and self.session is not None:
            note = ("STOPPED: the user said stop. Everything was cut off and nothing more will "
                    "be done. Do not continue, retry or resume the task" +
                    (" (the task plan was cancelled)" if had_task else "") +
                    ". Just say 'Stopped.' and wait.")
            responses = [types.FunctionResponse(
                id=i, name=n, response={"result": (finished[i] + " -- then: " + note) if i in finished else note})
                for i, n in pending]
            try:
                await self.session.send_tool_response(function_responses=responses)
            except Exception:
                log.debug("stop response failed", exc_info=True)
        self._state(self._idle_state())

    # --- was that meant for Mint? -------------------------------------------------

    _ADDR_OPTIONS = {
        "mint": "Said to Mint, the voice assistant: a request, a question, or a reply to it.",
        "other": "Said to someone else in the room, on a call, or to nobody in particular.",
    }
    _ADDR_INSTRUCTIONS = (
        "A voice assistant called Mint is listening in a room; the user is already talking with it. "
        "They just said `request`. Decide who it was said to. `mint_said` is Mint's last reply; "
        "`voice` says how loud it was compared with how they usually talk to Mint. Requests, questions "
        "or commands an assistant could act on (open, check, send, what's, remind, stop, and yes/no or "
        "follow-up answers to what Mint just said) are for Mint. Talk with another person "
        "(addressing someone by name, chit-chat, a phone call, reading aloud, thinking aloud, other "
        "languages spoken to people) is not.")

    def _begin_user_turn(self) -> None:
        """A new utterance from the user (already voice-verified if the lock is on)."""
        from mint.core import prefs
        self._suppress_turn = False
        self._goodbye_done = False
        self._addr_task = None
        self._addr_candidate = not self._expect_command and bool(prefs.get("addressee_check"))

    def _check_goodbye(self) -> None:
        """"Bye", said by the user, sends Mint to sleep - on the Mac, at once.
        Relying on the model missed it: "bye Mint" was transcribed "by mint",
        "by means", "by a mint", and it kept answering "I'm still here!"."""
        if self.asleep or self._goodbye_done or not _is_goodbye(self._heard):
            return
        self._goodbye_done = True
        self._print(f"[goodbye: {' '.join(self._heard.split())}]")
        # Let its own short goodbye play; stop listening now.
        if self.hands_free:
            self.go_to_sleep("you said bye")

    def _start_addressee_check(self) -> None:
        self._check_goodbye()
        if not self._suppress_turn and _foreign(self._heard):
            # Neither English nor Hindi (Latin or Devanagari script): not the user.
            self._suppress_turn = True
            self._flush_playback()
            self._print(f"[ignored: not English or Hindi: {self._heard[:60]}]")
            return
        if not self._addr_candidate or self._addr_task is not None or not self._heard.strip():
            return
        self._addr_task = asyncio.create_task(self._check_addressee(self._heard, list(self._turn_levels)))
        self._turn_levels = []

    async def _check_addressee(self, heard: str, levels: list[float]) -> None:
        from mint.app import control
        from mint.core import jev
        from mint.core import prefs
        from mint.voice import voicelock
        text = " ".join(heard.split())
        lowered = text.lower()
        if prefs.name().lower() in lowered or control.is_stop(text, prefs.get("stop_words")) or len(text) < 3:
            return
        voice = "normal"
        usual = voicelock.lock.level
        if usual and levels:
            ratio = float(np.median(levels)) / usual
            if ratio < 0.45:
                voice = "much quieter than usual - maybe turned away from the microphone"
            elif ratio > 1.8:
                voice = "louder than usual"
        started = time.monotonic()
        pick = await asyncio.to_thread(
            jev.choose, text, {k: v.replace("Mint", prefs.name()) for k, v in self._ADDR_OPTIONS.items()},
            {"mint_said": self._last_said[-300:], "voice": voice}, 4.0,
            self._ADDR_INSTRUCTIONS.replace("Mint", prefs.name()))
        took = time.monotonic() - started
        if pick is None:
            return                        # Jev unreachable: assume it was for Mint
        log.info("addressee %s p=%.2f c=%.2f in %.2fs: %s", pick.id, pick.probability, pick.confidence, took, text)
        if pick.id == "other" and pick.confidence >= 0.6:
            self._suppress_turn = True
            self._flush_playback()
            self._print(f"[not for me ({pick.confidence:.2f}, {took:.1f}s): {text[:80]}]")
            self._state(self._idle_state())

    def _flush_playback(self) -> None:
        while not self.audio_in.empty():
            self.audio_in.get_nowait()
        # The voice-processing player schedules audio ahead; stop it too.
        flush = getattr(self.audio, "flush", None)
        if flush is not None:
            flush()

    def _done_speaking(self) -> None:
        """Playback drained: reopen the mic and reset the idle clock."""
        if self.half_duplex:
            self.audio.muted = False
        self._last_voice = time.monotonic()
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
        if self._addr_task is not None and not self._addr_task.done():
            # Nothing is done on the screen until it is clear the words were
            # meant for Mint (about a second, only for follow-ups).
            await asyncio.wait({self._addr_task}, timeout=4.5)
        if self._suppress_turn and self.session is not None:
            await self.session.send_tool_response(function_responses=[
                types.FunctionResponse(id=f.id, name=f.name, response={"result": (
                    "NOT RUN: the user was talking to someone else, not to you. Do nothing and "
                    "say nothing.")}) for f in tool_call.function_calls])
            return
        from mint.app import control
        control.resume()                 # a new request: input is allowed again
        self._busy = True
        self._heard_during_work = False
        self._batch = [(f.id, f.name) for f in tool_call.function_calls]
        self._batch_results = {}
        opened = False
        try:
            for function in tool_call.function_calls:
                if control.stopped():
                    return
                name, args = function.name, dict(function.args or {})
                if name in _OPENERS and opened:
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
                    send_key = words if name in ("ui_act", "type_text") and args.get("press_return") and words else None
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
                        result, image = await self._run_one(name, args)
                        self._recent_calls[key] = (time.monotonic(), result)
                        if send_key:
                            sends[send_key] = (time.monotonic(), result)
                self._print(f"[{name}] {result[:160]}")
                from mint.knowledge.conversation import memory
                memory.add("tool", f"{name}({_describe(name, args)}) -> {result[:240]}")
                self._last_voice = time.monotonic()
                responses.append(types.FunctionResponse(
                    id=function.id, name=name, response={"result": result}))
                self._batch_results[function.id] = result
                from mint.app import autopilot
                autopilot.note(name, args, result)
                if image is not None:
                    images.append(image)
        except asyncio.CancelledError:
            return                       # stopped: stop_everything answers for the batch
        finally:
            self._busy = False
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

    async def _run_one(self, name: str, args: dict) -> tuple[str, dict | None]:
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
                from mint.ui.chat import summarize_text
                transcript = (await asyncio.to_thread(self.ui.chat_transcript) or "") or self._history_transcript()
                if not transcript.strip():
                    return "There is nothing to summarise yet.", None
                summary = await asyncio.to_thread(summarize_text, transcript)
                if summary.startswith("Could not summarise"):
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
            self.go_to_sleep("asked to stop")
            return "Going to sleep. Say the wake word to start again.", None

        if name == "set_timer":
            return self._start_timer(float(args.get("minutes", 1)), args.get("label", "")), None

        if name == "plan_task":
            steps = [str(s) for s in (args.get("steps") or []) if str(s).strip()]
            self.task = {"goal": str(args.get("goal", "")), "steps": steps, "done": {}}
            self._print(f"[task] {self.task['goal']}: " + " | ".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
            if steps:
                self.ui.progress(0, len(steps), f"Step 1/{len(steps)}: {steps[0][:60]}")
            # The skill and memories for this request, before the first step is
            # taken: without them the plan was made first and the known route
            # arrived too late (ZCode, in testing).
            try:
                from mint.tools import extra as extra_tools
                pack = await asyncio.to_thread(extra_tools.plan_context, self.task["goal"])
            except Exception:
                pack = ""
            return (f"Task started with {len(steps)} steps. Do step 1 now: {steps[0] if steps else ''}. "
                    "Call step_done after each step.") + pack, None

        if name == "step_done":
            return self._step_done(int(args.get("step", 0)), str(args.get("result", "")),
                                   bool(args.get("failed", False))), None

        from mint.ui import activity
        on_screen = activity.kind(name) in {"click", "type", "scroll", "open", "web", "switch"}
        self._state("working" if on_screen else "thinking", activity.phrase(name, args))
        self.ui.activity_start(name, args)
        if self.ui.needs_screen(name):
            # The chat had the keyboard; it was just handed back to the user's
            # app. Let that land before clicking or typing there.
            await asyncio.sleep(0.35)
        result, image = await tools.dispatch(name, args)
        self.ui.activity_end(name, not _looks_failed(result))
        return result + self._context_after(name), image

    def _set_preference(self, setting: str, value: str) -> str:
        from mint.core import prefs
        on = value.strip().lower() in {"on", "true", "yes", "enable", "enabled", "1", "show"}
        switches = {"spoken_replies": "voice", "microphone": "mic", "face": "face",
                    "effects": "cursor_effects", "word_animation": "word_animation",
                    "listen_while_working": "listen_while_working"}
        if setting in switches:
            prefs.set(switches[setting], on)
            if setting == "spoken_replies":
                return ("Spoken replies are ON: you speak again." if on else
                        "Spoken replies are OFF: from now on the user reads your replies in the chat "
                        "bubble instead of hearing them. Keep replies short and readable.")
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

    def _step_done(self, step: int, result: str, failed: bool) -> str:
        if not self.task or not (1 <= step <= len(self.task["steps"])):
            return "There is no such step in the current task."
        self.task["done"][step] = ("FAILED: " if failed else "") + result
        steps = self.task["steps"]
        remaining = [i for i in range(1, len(steps) + 1) if i not in self.task["done"]]
        self._print(f"[task] step {step}/{len(steps)} {'FAILED' if failed else 'done'}: {result[:100]}")
        if not remaining:
            summary = "; ".join(f"{i}. {self.task['done'][i]}" for i in range(1, len(steps) + 1))
            self.task = None
            self.ui.progress(len(steps), len(steps), "✓ task complete")
            self.ui.celebrate()
            if self.loop is not None:
                self.loop.call_later(4, lambda: self.ui.progress(0, 0))
            return f"All steps finished. Tell the user briefly what was done: {summary}"
        nxt = remaining[0]
        self.ui.progress(len(steps) - len(remaining), len(steps), f"Step {nxt}/{len(steps)}: {steps[nxt - 1][:60]}")
        return f"Recorded. Next is step {nxt}: {steps[nxt - 1]}."

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
            steps = self.task["steps"]
            remaining = [i for i in range(1, len(steps) + 1) if i not in self.task["done"]]
            if remaining:
                notes.append(f"[Task '{self.task['goal'][:60]}': {len(steps) - len(remaining)}/{len(steps)} "
                             f"steps done. Current: step {remaining[0]} - {steps[remaining[0] - 1]}. "
                             "Call step_done when it is finished.]")
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
        from mint.agents import runtime as agent_hub
        agent_hub.hub.attach(self)          # sub-agents report to this session
        client = _client()
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
        while True:
            try:
                settings = _live_config()
                if self._resume_handle:
                    settings.session_resumption = types.SessionResumptionConfig(
                        handle=self._resume_handle)
                async with client.aio.live.connect(model=config.MODEL, config=settings) as session:
                    self.session = session
                    attempt = 0
                    typing = " Type to send text; Ctrl-C to stop." if sys.stdin and sys.stdin.isatty() else ""
                    if self.hands_free:
                        word = self._wake_word.replace("_", " ")
                        print(f'\nMint is running (connected to {config.MODEL}). '
                              f'Say "{word}".{typing}\n', flush=True)
                    else:
                        print(f"\nMint is listening (connected to {config.MODEL}).{typing}\n", flush=True)
                    self._state(self._idle_state())
                    if self._pending_text:
                        asyncio.create_task(self._send_pending_text())

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
        self._state("offline", _short_error(message))
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
        if attempt >= 4 and transient and config.MODEL != config.FALLBACK_MODEL:
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
        await asyncio.sleep(delay)

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

    async def _unload_watch(self) -> None:
        """Asleep and idle long enough: unload, and let Mint Ear listen (~30 MB)."""
        from mint.app import ear
        from mint.core import prefs
        if not ear.managed() or self.text_mode:
            return
        while True:
            await asyncio.sleep(15)
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
        worker = threading.Thread(target=memory.summarize_now, daemon=True)
        worker.start()
        worker.join(timeout=8)


# --- helpers ---------------------------------------------------------------------

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
