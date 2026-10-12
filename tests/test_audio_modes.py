"""7 Oct: with Mint asleep (only listening for "Hey Mint"), a YouTube video or an anime played quieter and the mic
heard less: voice processing (echo cancellation) ran all the time, and macOS ducks every other app's sound while it
does. Now it runs only in a conversation: off asleep, on once the user has said their request, off again after."""
import asyncio
import threading
import time

try:
    from mint.voice import engine as audio_vp
except ImportError:
    from mint import audio_vp
try:
    from mint.app import session
except ImportError:
    from mint import session


def _audio(echo="auto", private=False, cancelled=False, playing=False):
    a = audio_vp.VoiceAudio.__new__(audio_vp.VoiceAudio)
    a._build_lock, a._lock = threading.RLock(), threading.Lock()
    a.resting = a.suspended = a._closed = a._deferred = False
    a.playing, a.echo_mode, a.private_output, a.echo_cancelled = playing, echo, private, cancelled
    a.conversation = a._relax_after_play = a._switching = False
    a.quiet_asleep = True
    a._restarting = a._restart_pending = False
    a._restarted_at, a._pending = 0.0, 0
    a._on_idle, a._loop = None, None
    a.switches = []
    a._switch_later = lambda reason: a.switches.append(reason)
    return a


def test_echo_cancellation_only_in_a_conversation_and_only_on_speakers():
    a = _audio()
    assert not a._wants_echo_cancel()                       # asleep: nothing ducked
    a.conversation = True
    assert a._wants_echo_cancel()                           # speakers, in a conversation
    a.private_output = True
    assert not a._wants_echo_cancel()                       # headphones: no echo to cancel
    a.private_output, a.echo_mode = False, "off"
    assert not a._wants_echo_cancel()
    a.echo_mode = "on"
    assert a._wants_echo_cancel()


def test_speaking_to_mint_brings_echo_cancellation_up_once():
    a = _audio()
    a.converse(True)
    a.converse(True)
    assert a.conversation and len(a.switches) == 1
    b = _audio(private=True)
    b.converse(True)
    assert b.conversation and b.switches == []               # headphones: already as it should be


def test_falling_asleep_goes_back_to_the_plain_mic_after_mint_has_spoken():
    a = _audio(cancelled=True, playing=True)
    a.conversation = True
    a.converse(False)
    assert a.switches == [] and a._relax_after_play          # not mid-sentence
    a._maybe_idle()                                          # its last words played
    assert a.switches == ["Mint is asleep: plain microphone"]
    b = _audio(cancelled=True)
    b.conversation = True
    b.converse(False)
    assert b.switches == ["Mint is asleep: plain microphone"]


def test_no_switch_while_paused_or_lent_to_a_call():
    a = _audio()
    a.resting = True
    a.converse(True)
    assert a.conversation and a.switches == []               # the next build reads the mode


def test_switches_are_spaced_and_skipped_when_no_longer_needed(monkeypatch):
    a = audio_vp.VoiceAudio.__new__(audio_vp.VoiceAudio)
    a._build_lock = threading.RLock()
    a._closed = a.suspended = a.resting = False
    a.conversation, a.echo_mode, a.private_output, a.echo_cancelled = True, "auto", False, False
    a.quiet_asleep = True
    a._restarted_at = time.monotonic()                       # rebuilt just now
    started = []
    a.restart = lambda reason: started.append(time.monotonic())
    monkeypatch.setattr(audio_vp, "MIN_GAP", 0.3)
    t0 = time.monotonic()
    a._switch_later("test")
    assert a._switching
    time.sleep(0.6)
    assert started and started[0] - t0 >= 0.25 and not a._switching
    a.conversation = False                                   # changed again before the switch
    a._restarted_at = 0.0
    started.clear()
    a._switch_later("test")
    time.sleep(0.2)
    assert started == [] and not a._switching


def test_a_reply_waits_for_the_switch_instead_of_being_dropped():
    a = _audio()
    a._started, a.silent, a.muted = True, False, False
    a.engine = a.out_engine = object()                       # echo cancellation: one engine
    a._switching = True
    scheduled = []

    class Player:
        def scheduleBuffer_completionHandler_(self, buffer, done):
            scheduled.append(time.monotonic())
    a.player = Player()
    a.play_format = audio_vp.A.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(24000, 1)

    async def run():
        q = asyncio.Queue()
        task = asyncio.create_task(a.play(q))
        await q.put(b"\x01\x00" * 240)
        await asyncio.sleep(0.3)
        assert scheduled == []                               # held while echo cancellation comes up
        a._switching = False
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(run())
    assert len(scheduled) == 1


def _mint(**kw):
    m = session.Mint.__new__(session.Mint)
    m.calls = []
    m.audio = type("A", (), {"converse": lambda self, on: m.calls.append(on)})()
    m.hands_free, m.asleep = True, False
    m.__dict__.update(kw)
    return m


def test_the_session_switches_with_the_conversation():
    m = _mint()
    m._converse(True)
    m._converse(False)
    assert m.calls == [True, False]
    always = _mint(hands_free=False)                         # always listening: never "asleep"
    always._converse(False)
    assert always.calls == []


def test_the_old_behaviour_is_one_setting_away():
    a = _audio()
    a.quiet_asleep = False                                   # settings: quiet_while_waiting false
    assert a._wants_echo_cancel()                            # echo cancellation even asleep, as before


def test_the_voice_lock_allows_for_the_plain_mic():
    try:
        from mint.voice import voicelock
    except ImportError:
        from mint import voicelock
    before = voicelock.strictness()
    plain = _audio()
    session.Mint._voice_allowance(plain)                     # asleep on speakers: the plain mic
    assert voicelock.capture_shift == voicelock.PLAIN_MIC and voicelock.strictness() == before + voicelock.PLAIN_MIC
    session.Mint._voice_allowance(_audio(cancelled=True))    # in a conversation: as the voiceprint was recorded
    assert voicelock.capture_shift == 0.0
    session.Mint._voice_allowance(_audio(private=True))      # headphones: always plain, voiceprint too
    assert voicelock.capture_shift == 0.0
    session.Mint._voice_allowance(_audio(echo="off"))
    assert voicelock.capture_shift == 0.0


class _Mic:
    """AVAudioInputNode's rule: installing a tap while one is installed throws ("nullptr == Tap()")."""

    def __init__(self):
        self.tap = None

    def installTapOnBus_bufferSize_format_block_(self, bus, size, fmt, block):
        if self.tap is not None:
            raise RuntimeError("required condition is false: nullptr == Tap()")
        self.tap = block

    def removeTapOnBus_(self, bus):
        self.tap = None


class _Engine:
    def __init__(self, fail_first=True):
        self.fail = fail_first

    def startAndReturnError_(self, _):
        if self.fail:
            self.fail = False
            raise RuntimeError("the engine threw half-way")     # after the tap was installed
        return True, None

    def stop(self):
        pass


def test_a_start_that_failed_half_way_does_not_jam_every_start_after_it():
    """12 Oct: a tap left by a failed start made every reconnect throw "nullptr == Tap()" - ~1,000 session drops
    an hour for six hours, and the battery drained. The tap is now cleared before it is installed again."""
    a = _audio()
    a.__class__ = type("Quiet", (audio_vp.VoiceAudio,), {"status": "test"})   # (the log line's summary)
    a.mic, a.engine = _Mic(), _Engine()
    a.out_engine = a.engine
    a.in_format, a._tap, a._started = None, object(), False
    a.player = type("P", (), {"play": lambda self: None})()
    a._observers = []
    try:
        a._start()
    except RuntimeError:
        pass
    assert a.mic.tap is not None and not a._started           # the failed start left its tap behind
    a._start()                                                 # used to throw "nullptr == Tap()" forever
    assert a._started


def test_pausing_mid_conversation_goes_back_to_the_plain_mic():
    """12 Oct: paused in the middle of a conversation, Mint resumed still in echo cancellation and stayed in it
    while asleep - the costly mode, all day. Pausing ends the conversation's microphone mode."""
    m = session.Mint.__new__(session.Mint)
    m.hands_free, m.loop, m.session = True, None, None
    m.audio = _audio(cancelled=True)
    m.audio.conversation = True
    m._print = lambda *a: None
    m.ui = type("UI", (), {"set_paused": lambda self, p: None})()
    m._audio_rest_later = lambda after=3.0: None
    m.set_paused(True)
    assert not m.audio.conversation and m.audio.switches == ["Mint is asleep: plain microphone"]


class _OutEngine:
    def __init__(self):
        self.running, self.starts, self.pauses = True, 0, 0

    def isRunning(self):
        return self.running

    def startAndReturnError_(self, _):
        self.running, self.starts = True, self.starts + 1
        return True, None

    def pause(self):
        self.running, self.pauses = False, self.pauses + 1


class _Player:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1

    def stop(self):
        pass


def test_the_speaker_side_pauses_when_idle_and_starts_again_to_speak(monkeypatch):
    """12 Oct: with the plain microphone the speaker engine played silence all day - the Mac kept from idle
    sleep, coreaudiod busy. It pauses after OUT_IDLE with nothing to say and starts again before a sound."""
    a = _audio()
    a.engine, a.out_engine, a.player = object(), _OutEngine(), _Player()
    a._started, a.playing = True, False
    a._out_wanted = 0.0
    a._rest_output()
    assert not a.out_engine.running and a.out_engine.pauses == 1
    assert a._ensure_output() and a.out_engine.running and a.out_engine.starts == 1 and a.player.plays == 1
    a._out_wanted = __import__("time").monotonic()            # just asked to play
    a._rest_output()
    assert a.out_engine.running                               # not paused under a sound about to play
    a.playing = True
    a._out_wanted = 0.0
    a._rest_output()
    assert a.out_engine.running                               # never while speaking
    a.flush()                                                 # cut off: the player restarts only on a running engine
    a.out_engine.running = False
    plays = a.player.plays
    a.flush()
    assert a.player.plays == plays


def test_with_echo_cancellation_the_one_engine_keeps_running():
    a = _audio(cancelled=True)
    shared = _OutEngine()
    a.engine = a.out_engine = shared
    a.player, a._started, a.playing = _Player(), True, False
    a._rest_output()
    assert shared.running and shared.pauses == 0               # the microphone is on that engine too
