"""Microphone and speaker through one AVAudioEngine, with macOS voice processing.

Voice processing is the echo canceller FaceTime uses. Because the microphone
and the speaker belong to the same engine, the system knows exactly what is
being played and subtracts it from what the microphone hears. That is what
lets the microphone stay open while Mint talks - and so lets the user cut in.

Living alongside other apps (calls, music, meetings) shaped the rest:

* Voice processing ducks every other app's audio. The adjustable part is set
  to its minimum; the flat -15 dB applied when the unit is created is lifted
  for Mint's process (audio_devices.unduck).
* With headphones or AirPods there is no echo to cancel, so in "auto" mode
  voice processing is simply off - no ducking at all, no hidden aggregate
  device.
* Only during a conversation (7 Oct): asleep - listening for "Hey Mint" while the
  user watches a video - voice processing is off too, so other apps play at full
  volume and the microphone hears as it is. It comes on once the user has said
  their request (converse(True): ~1 s to build, while the model thinks) so Mint
  can be talked over, and goes off when Mint falls asleep again.
* The engine stops itself whenever the audio setup changes (a device plugged
  in, another app starting voice processing). Before, Mint then went deaf
  without a word; now it rebuilds, and a watchdog restarts a silent mic.
* suspend()/resume() release the microphone entirely - used while a call or
  meeting app has it (see session._mic_share_watch).
* The microphone and speaker can be chosen (settings "input_device" /
  "output_device", CoreAudio UIDs); empty means the system default.

Same interface as audio.Audio: listen(handler), play(queue, on_idle, on_chunk),
flush(), close(), and the `muted` / `playing` flags.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
import time

import AVFoundation as A
import numpy as np

from mint.voice import devices as audio_devices
from mint.core import config

log = logging.getLogger("mint.voice.audio")

_au = ctypes.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
_au.AudioUnitSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32]
_au.AudioUnitSetProperty.restype = ctypes.c_int32
_CURRENT_DEVICE = 2000        # kAudioOutputUnitProperty_CurrentDevice
_SCOPE_GLOBAL = 0
_DUCK_MIN = 10                # AVAudioVoiceProcessingOtherAudioDuckingLevelMin
MIN_GAP = 2.0                 # seconds between two rebuilds of the engine


def _resample(samples: np.ndarray, source: float, target: float) -> np.ndarray:
    """Resample mono float audio. For 48k -> 16k, average then decimate (a
    cheap low-pass, enough for speech); otherwise interpolate."""
    if source == target or samples.size == 0:
        return samples
    ratio = source / target
    if abs(ratio - round(ratio)) < 1e-6 and ratio >= 2:
        step = int(round(ratio))
        usable = samples[: samples.size - samples.size % step]
        return usable.reshape(-1, step).mean(axis=1)
    positions = np.arange(0, samples.size, ratio)
    return np.interp(positions, np.arange(samples.size), samples)


class VoiceAudio:
    def __init__(self, echo: str = "auto", input_uid: str = "", output_uid: str = "", defer: bool = False,
                 quiet_asleep: bool = True) -> None:
        self.muted = False
        self.playing = False
        self.echo_mode = echo                  # auto | on | off
        self.input_uid, self.output_uid = input_uid, output_uid
        self.echo_cancelled = False
        self.private_output = False            # headphones: nothing said reaches the mic
        self.suspended = False
        self.silent = False                    # a Google Meet call has Mint's voice: the speaker plays silence
        self.resting = False                   # the mic is off (Mint paused): no engine running for nothing
        self.conversation = False              # in a conversation: echo cancellation may run (converse())
        self.quiet_asleep = quiet_asleep       # asleep: the plain mic (False: echo cancellation all the time)
        self._relax_after_play = False         # go back to the plain mic once Mint has finished speaking
        self._switching = False                # a mode switch is scheduled: a reply waits for it
        self._restarting = False               # a rebuild is under way (it takes ~2 s with voice processing)
        self._restarted_at = 0.0
        self.on_reconfigure = None             # callback(audio) after every rebuild
        self.status = ""
        self._pending = 0
        self._lock = threading.Lock()
        self._build_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic_queue: asyncio.Queue | None = None
        self._on_idle = None
        self._idle_timer: threading.Timer | None = None
        self._observers: list = []
        self._started = False
        self._last_tap = 0.0
        self._restart_pending = False
        self._settle_until = 0.0               # our own rebuild's notifications, ignored
        self._closed = False
        self.play_format = A.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
            config.RECEIVE_SAMPLE_RATE, 1)
        # `defer`: Mint Ear is handing over the user's words from its own
        # microphone, and building voice processing makes every other reader
        # of the microphone get digital silence (tested: 0.7 s of the sentence,
        # then zeros). So the engine is built at the first start, once the user
        # has paused.
        self._deferred = defer
        if defer:
            self.status = "starting after the hand-over"
        else:
            self._build()

    # --- building the engine ------------------------------------------------------

    @property
    def full_duplex(self) -> bool:
        """The mic can stay open while Mint talks without hearing itself."""
        return self.echo_cancelled or self.private_output

    def _devices(self) -> tuple[int | None, int | None]:
        wanted_in = audio_devices.device_by_uid(self.input_uid) if self.input_uid else None
        wanted_out = audio_devices.device_by_uid(self.output_uid) if self.output_uid else None
        if self.input_uid and not wanted_in:
            log.warning("chosen microphone %s is not connected; using the system default", self.input_uid)
        if self.output_uid and not wanted_out:
            log.warning("chosen speaker %s is not connected; using the system default", self.output_uid)
        in_id = wanted_in["id"] if wanted_in else audio_devices.default_device(output=False)
        out_id = wanted_out["id"] if wanted_out else audio_devices.default_device(output=True)
        return in_id, out_id

    def _build(self) -> None:
        # Building (voice processing, device choice) posts the engine's own
        # "configuration changed" - sometimes before start() - so the quiet
        # period begins here.
        self._settle_until = time.monotonic() + 2.5
        in_id, out_id = self._devices()
        out = audio_devices.describe(out_id)
        self.private_output = audio_devices.is_private_listening(out)
        want_vp = self._wants_echo_cancel()

        # With voice processing, one engine: the canceller must see what is
        # played to subtract it. Without, two - one records, one plays - since
        # a single engine without voice processing cannot use different
        # devices for input and output (in testing the mic went silent, or
        # Mint's voice went to the chosen mic's device).
        self.engine = A.AVAudioEngine.alloc().init()
        self.mic = self.engine.inputNode()
        self.out_engine = self.engine if want_vp else A.AVAudioEngine.alloc().init()
        self.player = A.AVAudioPlayerNode.alloc().init()
        self.out_engine.attachNode_(self.player)
        self.out_engine.connect_to_format_(self.player, self.out_engine.mainMixerNode(), self.play_format)

        self.echo_cancelled = False
        if want_vp:
            # Only after the output is connected: enabled first, the engine
            # could not initialise its output (-10875) in testing.
            ok, error = self.mic.setVoiceProcessingEnabled_error_(True, None)
            if ok:
                self.echo_cancelled = True
                try:
                    # Duck other apps as little as the API allows.
                    self.mic.setVoiceProcessingOtherAudioDuckingConfiguration_((False, _DUCK_MIN))
                except Exception as err:
                    log.debug("ducking configuration not set: %s", err)
            else:
                log.warning("voice processing unavailable (%s); echo cancellation off", error)
        self._select_devices(in_id, out_id)

        self.in_format = self.mic.outputFormatForBus_(0)
        self.in_rate = float(self.in_format.sampleRate())
        # The engine stops itself when the audio setup changes; rebuild then.
        from Foundation import NSNotificationCenter
        center = NSNotificationCenter.defaultCenter()
        self._observers = [center.addObserverForName_object_queue_usingBlock_(
            A.AVAudioEngineConfigurationChangeNotification, engine, None,
            lambda note: self._schedule_restart("the audio setup changed"))
            for engine in {id(self.engine): self.engine, id(self.out_engine): self.out_engine}.values()]
        self.status = self._describe(in_id, out_id)
        log.info("audio: %s", self.status)

    def _wants_echo_cancel(self) -> bool:
        """Voice processing for this build: speakers (or forced on), and only in a conversation - asleep it would
        duck every other app's sound and change what the microphone hears, for nothing (nobody to cancel)."""
        return (self.conversation or not self.quiet_asleep) and \
            (self.echo_mode == "on" or (self.echo_mode == "auto" and not self.private_output))

    @property
    def plain_on_speakers(self) -> bool:
        """The plain mic where the voiceprint was recorded with echo cancellation (voicelock.capture_shift)."""
        return not self.echo_cancelled and not self.private_output and self.echo_mode != "off"

    def converse(self, on: bool) -> None:
        """Any thread, never blocks. on: the user has spoken to Mint - bring echo cancellation up (if this setup
        uses it) so they can talk over Mint's reply. off: Mint is asleep again - back to the plain microphone, once
        it has finished speaking."""
        with self._build_lock:
            if on == self.conversation:
                return
            self.conversation = on
            if self._closed or self.suspended or self.resting or self._deferred:
                return                         # the next build reads self.conversation
            if self._wants_echo_cancel() == self.echo_cancelled:
                return                         # headphones, or echo off: nothing to change
            if not on and self.playing:
                self._relax_after_play = True  # after Mint's last words (_maybe_idle)
                return
        self._switch_later("a conversation started" if on else "Mint is asleep: plain microphone")

    def _switch_later(self, reason: str) -> None:
        """Rebuild for the new mode, at least MIN_GAP after the last rebuild (two rebuilds in quick succession
        crashed the audio unit once: SIGSEGV in AVAudioIOUnit, 6 Oct)."""
        wait = max(0.0, MIN_GAP - (time.monotonic() - self._restarted_at))
        self._switching = True

        def switch() -> None:
            try:
                with self._build_lock:
                    if self._closed or self.suspended or self.resting or \
                            self._wants_echo_cancel() == self.echo_cancelled:
                        return                 # changed again meanwhile, or not needed any more
                self.restart(reason)
            finally:
                self._switching = False
        timer = threading.Timer(wait, switch)
        timer.daemon = True
        timer.start()

    def _select_devices(self, in_id: int | None, out_id: int | None) -> None:
        """Point the engine at the chosen devices. With voice processing both
        sides are set together on the one voice unit (input element 1, output
        element 0), before it starts - Apple's rules for that unit."""
        if not self.input_uid and not self.output_uid:
            return                       # system defaults: leave the engine alone
        try:
            if self.echo_cancelled:
                unit = self.mic.audioUnit()
                pointer = ctypes.c_void_p(unit.pointerAsInteger)
                for element, device in ((1, in_id), (0, out_id)):
                    if device:
                        value = ctypes.c_uint32(device)
                        status = _au.AudioUnitSetProperty(pointer, _CURRENT_DEVICE, _SCOPE_GLOBAL, element,
                                                          ctypes.byref(value), 4)
                        if status != 0:
                            log.warning("could not select device %s on element %s (%s)", device, element, status)
            else:
                if self.output_uid and out_id:
                    self.out_engine.outputNode().AUAudioUnit().setDeviceID_error_(out_id, None)
                if self.input_uid and in_id:
                    self.mic.AUAudioUnit().setDeviceID_error_(in_id, None)
                    if self.mic.AUAudioUnit().deviceID() != in_id:
                        log.warning("microphone choice did not take; using the system default")
        except Exception as error:
            log.warning("device selection failed (%s); using system defaults", error)

    def _describe(self, in_id, out_id) -> str:
        name = lambda d: (audio_devices.describe(d) or {}).get("name", "?")
        mode = "echo cancellation on" if self.echo_cancelled else (
            "echo cancellation off (headphones)" if self.private_output else "echo cancellation off")
        return f"mic {name(in_id)}, speaker {name(out_id)}, {mode}"

    def _teardown(self) -> None:
        try:
            if self._started:
                self.mic.removeTapOnBus_(0)
            self.engine.stop()
            if self.out_engine is not self.engine:
                self.out_engine.stop()
        except Exception:
            pass
        from Foundation import NSNotificationCenter
        for observer in self._observers:
            NSNotificationCenter.defaultCenter().removeObserver_(observer)
        self._observers = []
        self._started = False
        with self._lock:
            self._pending = 0
        self.playing = False

    def _start(self) -> None:
        with self._build_lock:
            if self._started or self.suspended or self.resting:
                return
            if self._deferred:
                self._deferred = False
                self._build()
                if self.on_reconfigure is not None:
                    self.on_reconfigure(self)
            self.mic.installTapOnBus_bufferSize_format_block_(0, 4800, self.in_format, self._tap)
            ok, error = self.engine.startAndReturnError_(None)
            if ok and self.out_engine is not self.engine:
                ok, error = self.out_engine.startAndReturnError_(None)
            if not ok:
                self.mic.removeTapOnBus_(0)
                self.engine.stop()
                raise RuntimeError(f"audio engine failed to start: {error}")
            self.player.play()
            self._started = True
            self._last_tap = time.monotonic()
            # Starting (and choosing devices) makes the engine post its own
            # "configuration changed"; reacting to that looped rebuilds forever
            # in testing. Real changes after this are still caught, and the
            # silent-mic watchdog covers anything missed.
            self._settle_until = time.monotonic() + 2.0
        if self.echo_cancelled:
            # macOS ducks the other apps' output by a flat -15 dB when a voice
            # unit comes up, beyond the configurable part; lift it for this
            # process, now and once more after the unit settles.
            audio_devices.unduck()
            threading.Timer(1.0, audio_devices.unduck).start()
        log.info("audio started: %s", self.status)

    def restart(self, reason: str) -> None:
        """Rebuild the engine from scratch (new devices, new mode, or it stopped)."""
        with self._build_lock:
            if self._closed or self._deferred:
                return                         # deferred: the first start builds with the new settings
            self._restart_pending = False
            self._restarting = True
            log.warning("audio: restarting (%s)", reason)
            print(f"  [audio: restarting - {reason}]", flush=True)
            self._teardown()
            try:
                self._build()
                self._start()
            except Exception as error:
                log.error("audio restart failed: %s", error)
                print(f"  [audio: could not restart the microphone: {error}]", flush=True)
            finally:
                self._restarting = False
                self._restarted_at = time.monotonic()
            if self.on_reconfigure is not None:
                try:
                    self.on_reconfigure(self)
                except Exception:
                    log.exception("on_reconfigure failed")

    def _schedule_restart(self, reason: str) -> None:
        """From any thread; several notifications in a burst make one restart."""
        if self._closed or self._restart_pending or self.suspended or self.resting or self._restarting \
                or time.monotonic() < self._settle_until:
            return
        self._restart_pending = True
        threading.Timer(0.5, lambda: self.restart(reason)).start()

    def configure(self, echo: str | None = None, input_uid: str | None = None,
                  output_uid: str | None = None) -> None:
        """New settings from the user: apply them with a rebuild."""
        if echo is not None:
            self.echo_mode = echo
        if input_uid is not None:
            self.input_uid = input_uid
        if output_uid is not None:
            self.output_uid = output_uid
        self.restart("settings changed")

    def suspend(self, reason: str = "") -> None:
        """Let go of the microphone and speaker entirely (a call needs them)."""
        with self._build_lock:
            if self.suspended:
                return
            self.suspended = True
            self._teardown()
        log.info("audio suspended: %s", reason)

    def resume(self) -> None:
        with self._build_lock:
            if not self.suspended:
                return
            self.suspended = False
            if self.resting:
                return                         # the mic is off anyway: it starts when needed (wake)
        self.restart("microphone free again")

    def rest(self) -> None:
        """The mic is off (Mint paused) and nothing is playing: stop the engine. Running, voice processing cost
        ~15% of a core while listening to nothing (6 Oct). wake() brings it back - for the mic, or to speak."""
        with self._build_lock:
            if self.resting or self.suspended or self._closed or self._deferred or self.playing:
                return
            self.resting = True
            self._teardown()
        log.info("audio resting: the microphone is off")

    def wake(self) -> None:
        with self._build_lock:
            if not self.resting:
                return
            self.resting = False
            if self.suspended:
                return
        self.restart("the microphone is on again, or Mint speaks")

    # --- capture ---------------------------------------------------------------

    def _tap(self, buffer, when) -> None:
        """Audio thread: convert to 16 kHz 16-bit mono and hand to the loop."""
        self._last_tap = time.monotonic()
        if self.muted or self._loop is None or self._mic_queue is None:
            return
        frames = buffer.frameLength()
        if frames == 0:
            return
        samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(frames), dtype=np.float32)
        mono16k = _resample(samples, self.in_rate, config.SEND_SAMPLE_RATE)
        pcm = (np.clip(mono16k, -1, 1) * 32767).astype(np.int16).tobytes()
        self._loop.call_soon_threadsafe(self._offer, pcm)

    def _offer(self, pcm: bytes) -> None:
        try:
            self._mic_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    async def listen(self, handler) -> None:
        self._loop = asyncio.get_running_loop()
        self._mic_queue = asyncio.Queue(maxsize=200)
        self._start()
        while True:
            try:
                pcm = await asyncio.wait_for(self._mic_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                # Watchdog: a running engine delivers audio every 0.1 s. Silence
                # for seconds means it stopped under us; bring it back.
                if self.resting or self._restarting or time.monotonic() - self._restarted_at < 6.0:
                    # (a rebuild right after another one tore down an audio unit macOS still had a callback
                    # queued for: SIGSEGV in AVAudioIOUnit::_GetHWFormat, 6 Oct)
                    continue
                if (self._started and not self.suspended and not self._restart_pending
                        and time.monotonic() - self._last_tap > 3.0):
                    await asyncio.to_thread(self.restart, "the microphone went silent")
                elif not self._started and not self.suspended:
                    await asyncio.to_thread(self.restart, "the microphone was not running")
                continue
            await handler(pcm)

    # --- playback ----------------------------------------------------------------

    def _finished_one(self) -> None:
        """Completion of one scheduled buffer (audio thread)."""
        with self._lock:
            self._pending = max(0, self._pending - 1)
            idle = self._pending == 0
        if idle:
            # A short grace period, so gaps between streamed chunks are not idle.
            if self._idle_timer:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(0.35, self._maybe_idle)
            self._idle_timer.start()

    def _maybe_idle(self) -> None:
        with self._lock:
            if self._pending:
                return
        self.playing = False
        if self._relax_after_play:
            self._relax_after_play = False
            if not self.conversation:
                self._switch_later("Mint is asleep: plain microphone")
        if self._on_idle is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_idle)

    async def play(self, in_queue: asyncio.Queue, on_idle=None, on_chunk=None) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_idle = on_idle
        self._start()
        while True:
            chunk = await in_queue.get()
            if self.resting:
                await asyncio.to_thread(self.wake)     # the mic is off but Mint has something to say
            waited = 0.0
            while (self._restarting or self._restart_pending or self._switching) and not self.suspended \
                    and waited < 4.0:
                await asyncio.sleep(0.05)      # a rebuild (e.g. echo cancellation coming up): the reply waits
                waited += 0.05
            if self.suspended or not self._started:
                continue                       # a call has the speaker; say nothing
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768
            if samples.size == 0:
                continue
            if self.silent:
                samples[:] = 0                 # still played, so the timing (playing, idle) stays the same
            buffer = A.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self.play_format, samples.size)
            buffer.setFrameLength_(samples.size)
            np.frombuffer(buffer.floatChannelData()[0].as_buffer(samples.size), dtype=np.float32)[:] = samples
            with self._lock:
                self._pending += 1
            self.playing = True
            if on_chunk is not None:
                on_chunk(chunk)
            player = self.player
            player.scheduleBuffer_completionHandler_(buffer, lambda: self._finished_one())

    def set_silent(self, on: bool) -> None:
        self.silent = on

    def duck(self, on: bool) -> None:
        """Turn Mint's voice down while it is not yet sure the voice cutting
        in is the user's (the voice lock needs ~1 s); back up if it is not."""
        if self._deferred:
            return
        self.player.setVolume_(0.25 if on else 1.0)

    def flush(self) -> None:
        """Stop speaking now: drop everything scheduled (the user cut in)."""
        with self._lock:
            self._pending = 0
        if self._started:
            self.player.stop()
            self.player.play()
        self.playing = False

    def close(self) -> None:
        with self._build_lock:
            self._closed = True
            self._teardown()
