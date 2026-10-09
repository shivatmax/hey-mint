"""Dropped Live connections (6 Oct): Google ends each connection after about an hour and warns first; one user's
question died with the connection and the caption said the service was down. Reconnect before the end, keep the
conversation, don't alarm on one blip, and send the user's last words again."""
import asyncio
import datetime as dt
import time

try:
    from mint.app import session
except ImportError:
    from mint import session


def test_seconds_from_any_duration():
    assert session._seconds(dt.timedelta(seconds=50)) == 50
    assert session._seconds("12s") == 12

    class Duration:
        seconds, nanos = 7, 500_000_000
    assert session._seconds(Duration()) == 7.5
    assert session._seconds(None) == 60 and session._seconds("soon") == 60


def test_routine_drops_are_told_apart_from_real_trouble():
    for message in ("1011 None. Deadline expired before operation could complete.", "1011 None. Internal error",
                    "1008 None. The operation was aborted.", "1000 None.", "sent 1001 (going away)"):
        assert session._routine_drop(message), message
    for message in ("1008 API key not valid", "1011 You exceeded your current quota", "1007 Invalid argument",
                    "429 RESOURCE_EXHAUSTED", "PERMISSION_DENIED"):
        assert not session._routine_drop(message), message


class _Session:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _mint(**kw):
    m = session.Mint.__new__(session.Mint)
    m.session = _Session()
    m._restarting = False
    m._resume_handle = "handle-1"
    m.asleep = False
    m._heard, m._said, m._spoke_at, m._model_active_at = "", "", 0.0, 0.0
    m.printed, m.states, m.told = [], [], []
    m._print = m.printed.append
    m._state = lambda *a: m.states.append(a)
    m._tell_outage = m.told.append
    m.__dict__.update(kw)
    return m


def test_first_blip_reconnects_quietly_and_the_second_is_an_outage(monkeypatch):
    m = _mint()

    async def run():
        await m._backoff(RuntimeError("1011 None. Deadline expired before operation could complete."), 1)
        quiet = (list(m.states), list(m.told))
        monkeypatch.setattr(m, "_count_down", lambda delay, why: asyncio.sleep(0), raising=False)
        await m._backoff(RuntimeError("1011 None. Internal error encountered."), 3)
        return quiet
    (states, told) = asyncio.run(run())
    assert states == [("offline", "Reconnecting…")] and told == []           # no alarm, no spoken notice
    assert m.told == ["The voice service is down on Google's side"]           # a repeat failure is an outage


def test_go_away_reconnects_at_a_quiet_moment_with_the_handle(monkeypatch):
    try:
        from mint.app import compaction
    except ImportError:
        from mint import compaction
    busy = {"on": True}
    monkeypatch.setattr(compaction, "busy_reason", lambda mint: "speaking" if busy["on"] else "")
    m = _mint()
    original = m.session

    async def run():
        class GoAway:
            time_left = dt.timedelta(seconds=30)
        m._go_away(GoAway())
        await asyncio.sleep(0.6)
        assert not original.closed                       # Mint is talking: wait
        busy["on"] = False
        await asyncio.wait_for(m._go_away_task, 5)
    asyncio.run(run())
    assert original.closed and m._restarting and m._resume_handle == "handle-1"   # same conversation, resumed
    assert any("reconnecting at a quiet moment" in p for p in m.printed)


def test_go_away_does_not_wait_past_the_deadline(monkeypatch):
    try:
        from mint.app import compaction
    except ImportError:
        from mint import compaction
    monkeypatch.setattr(compaction, "busy_reason", lambda mint: "speaking")
    m = _mint()
    original = m.session
    started = time.monotonic()
    asyncio.run(m._reconnect_before(3.5, original))      # never quiet: still reconnects before the end
    assert original.closed and time.monotonic() - started < 2


def test_words_lost_in_a_drop_are_sent_again():
    now = time.monotonic()
    m = _mint(_heard=" What are you  doing? ", _spoke_at=now - 3)
    assert m._lost_words() == "What are you doing?"
    assert _mint(_heard="open Safari", _spoke_at=now - 3, _model_active_at=now - 1)._lost_words() == ""  # answered
    assert _mint(_heard="hello", _said="Hi Boss", _spoke_at=now - 3)._lost_words() == ""
    assert _mint(_heard="hello", _spoke_at=now - 60)._lost_words() == ""                                # too old
    assert _mint(_heard="hello", _spoke_at=now - 3, asleep=True)._lost_words() == ""


def test_the_warning_from_google_starts_the_reconnect(monkeypatch):
    m = _mint()
    seen = []
    monkeypatch.setattr(m, "_go_away", seen.append, raising=False)

    class GoAway:
        time_left = "40s"

    class Response:
        usage_metadata = session_resumption_update = None
        go_away = GoAway()
    asyncio.run(m._handle(Response()))
    assert seen and seen[0] is Response.go_away


def test_go_away_waits_for_a_calm_moment_not_just_a_gap(monkeypatch):
    try:
        from mint.app import compaction
    except ImportError:
        from mint import compaction
    monkeypatch.setattr(compaction, "busy_reason", lambda mint: "")
    m = _mint()
    original = m.session
    started = time.monotonic()
    asyncio.run(m._reconnect_before(30, original))
    assert original.closed and time.monotonic() - started >= 1.4       # 1.5 s of calm first


def _listener(monkeypatch, **kw):
    monkeypatch.setattr(session, "ANSWER_NUDGE", 0.05)
    monkeypatch.setattr(session, "ANSWER_RESEND", 0.1)
    fields = dict(_dropping=False, _suppress_turn=False, _seg=0, _verdict=None, _typed_turn=False,
                  _turn_heard_at=time.monotonic(), _after_wake=True, _heard="Tell me what skills you have.")
    m = _mint(**{**fields, **kw})
    m.closed_audio, m.sent = [], []

    async def close_audio():
        m.closed_audio.append(True)

    async def inject(text, asked=None):
        m.sent.append(text)
    m._close_user_audio, m.inject_text = close_audio, inject
    return m


def test_words_that_get_no_answer_are_closed_then_sent_as_text(monkeypatch):
    """6 Oct: the transcript showed, then nothing - Gemini never heard the user's words end."""
    m = _listener(monkeypatch)
    asyncio.run(m._voice_answer_watch(time.monotonic()))
    assert m.closed_audio == [True] and m.sent == ["Tell me what skills you have."]


def test_an_answer_ends_the_watch(monkeypatch):
    m = _listener(monkeypatch)

    async def run():
        task = asyncio.create_task(m._voice_answer_watch(time.monotonic()))
        await asyncio.sleep(0.08)                       # after the nudge, the model answers
        m._model_active_at = time.monotonic()
        await task
    asyncio.run(run())
    assert m.closed_audio == [True] and m.sent == []
    m = _listener(monkeypatch, _model_active_at=time.monotonic() + 1)    # answered before the nudge
    asyncio.run(m._voice_answer_watch(time.monotonic()))
    assert m.closed_audio == [] and m.sent == []


def test_only_words_said_right_after_the_wake_word_are_resent(monkeypatch):
    m = _listener(monkeypatch, _after_wake=False)      # a follow-up: may not have been for Mint
    asyncio.run(m._voice_answer_watch(time.monotonic()))
    assert m.closed_audio == [True] and m.sent == []
    m = _listener(monkeypatch, _typed_turn=True)
    asyncio.run(m._voice_answer_watch(time.monotonic()))
    assert m.closed_audio == [] and m.sent == []
    m = _listener(monkeypatch, _suppress_turn=True)    # judged not for Mint
    asyncio.run(m._voice_answer_watch(time.monotonic()))
    assert m.closed_audio == [] and m.sent == []


def test_the_end_of_the_users_voice_starts_the_watch(monkeypatch):
    m = _listener(monkeypatch)
    started = []
    monkeypatch.setattr(m, "_voice_answer_watch", lambda spoke: started.append(spoke) or asyncio.sleep(0),
                        raising=False)
    monkeypatch.setattr(m, "_voice_watch", lambda: asyncio.sleep(0), raising=False)

    async def run():
        m.loop = asyncio.get_running_loop()
        m._voice_sent(time.monotonic())
        await asyncio.sleep(0.01)
        return m._answer_pending()
    asyncio.run(run())
    assert len(started) == 1


def test_words_already_judged_are_left_alone(monkeypatch):
    m = _listener(monkeypatch, _verdict="wait")         # "I want to…": waiting for the rest on purpose
    asyncio.run(m._voice_answer_watch(time.monotonic()))
    assert m.closed_audio == [] and m.sent == []


def test_what_counts_as_too_slow():
    assert session._too_slow([0.6, 0.7, 0.9]) == ""
    assert session._too_slow([0.6, 3.0]) == ""                         # too few to judge
    assert "3.1" in session._too_slow([0.8, 3.1, 4.0])                # the median of the last 3
    assert session._too_slow([0.7, 16.9, 0.9, 0.8]) == ""             # one stall is not a pattern
    assert "2 answers" in session._too_slow([0.7, 16.9, 0.9, 0.8, 15.2, 0.6])


import pytest  # noqa: E402

try:
    from mint.voice import live_models
except ImportError:
    from mint import live_models


@pytest.fixture
def models(monkeypatch, tmp_path):
    """A pool of three made-up models, its state in a temp file, and the session on the first."""
    try:
        from mint.core import config, prefs
    except ImportError:
        from mint import config, prefs
    live_models.reset_for_tests(tmp_path / "voice-models.json")
    monkeypatch.setattr(live_models, "POOL", ("models/a-live", "models/b-live", "models/c-live"))
    monkeypatch.delenv("MINT_MODEL", raising=False)
    monkeypatch.setattr(prefs, "get", lambda key, *a: None)
    monkeypatch.setattr(config, "MODEL", "models/a-live")
    yield config
    live_models.reset_for_tests()


def test_slow_answers_bench_the_model_and_move_on(models):
    m = _mint()
    moved = []

    async def switch(why, at_once=False):
        moved.append((why, at_once))
    m._switch_model = switch

    async def run():
        m.loop = asyncio.get_running_loop()
        for took in (0.8, 2.6, 2.9, 3.1):
            m._asked_at = time.monotonic() - took
            m._note_latency(time.monotonic())
            await asyncio.sleep(0)
    asyncio.run(run())
    assert len(moved) == 1 and "answers took" in moved[0][0] and moved[0][1] is False   # at a quiet moment
    assert live_models.benched("models/a-live") > 4 * 60


def test_the_switch_waits_for_quiet_and_reconnects_fresh(models, monkeypatch):
    try:
        from mint.app import compaction
    except ImportError:
        from mint import compaction
    monkeypatch.setattr(compaction, "busy_reason", lambda mint: "")
    live_models.strike("models/a-live", "slow")
    m = _mint()
    original = m.session
    asyncio.run(m._switch_model("answers took 3.4 s"))
    assert models.MODEL == "models/b-live" and original.closed and m._restarting and m._resume_handle is None


def test_the_first_sound_of_an_answer_is_timed_once(monkeypatch, models):
    m = _mint()
    seen = []

    class Stop(Exception):
        pass

    def note(now):
        seen.append(now)
        raise Stop
    monkeypatch.setattr(m, "_note_latency", note, raising=False)
    m._suppress_turn = m._hush = False

    class Response:
        usage_metadata = session_resumption_update = go_away = tool_call = server_content = None
        data = b"\0\0"
    try:
        asyncio.run(m._handle(Response()))
    except Stop:
        pass
    assert len(seen) == 1                                   # the model's answer is timed as it begins

    m = _mint()
    m._asked_at = time.monotonic() - 1.0
    m._note_latency(time.monotonic())
    m._note_latency(time.monotonic() + 5)                   # more of the same answer: not a new request
    assert len(m._latencies) == 1 and 0.9 < m._latencies[0] < 1.5


def _stalling(models, **kw):
    fields = dict(_busy=False, _unanswered="", _dropping=False, _suppress_turn=False, _seg=0,
                  _heard="Tell me what skills you have.", _after_wake=True)
    m = _mint(**{**fields, **kw})
    m.switches = []

    async def switch(why, at_once=False):
        m.switches.append((why, at_once))
    m._switch_model = switch
    return m


def test_a_request_with_no_answer_moves_to_the_next_model_at_once(models, monkeypatch):
    monkeypatch.setattr(session, "SLOW_STALL", 0.05)
    m = _stalling(models)

    async def run():
        m.loop = asyncio.get_running_loop()
        m._asked_at = asked = time.monotonic()
        await m._stall_watch(asked)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert m.switches == [("no answer in 0.05 s", True)]
    assert m._unanswered == "Tell me what skills you have."             # asked again on the next model
    assert live_models.benched("models/a-live") > 0


def test_no_switch_when_it_answered_or_a_tool_runs(models, monkeypatch):
    monkeypatch.setattr(session, "SLOW_STALL", 0.05)
    for kw, asked_now in (({}, False), ({"_busy": True}, True)):
        m = _stalling(models, **kw)

        async def run():
            m.loop = asyncio.get_running_loop()
            asked = time.monotonic()
            m._asked_at = asked if asked_now else 0.0      # 0: answered (or stopped) meanwhile
            await m._stall_watch(asked)
            await asyncio.sleep(0)
        asyncio.run(run())
        assert m.switches == [], kw
    assert live_models.benched("models/a-live") == 0


def test_a_follow_up_is_not_resent_as_a_request(models, monkeypatch):
    monkeypatch.setattr(session, "SLOW_STALL", 0.05)
    m = _stalling(models, _after_wake=False)

    async def run():
        m.loop = asyncio.get_running_loop()
        m._asked_at = asked = time.monotonic()
        await m._stall_watch(asked)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert m.switches and m._unanswered == ""


# --- the pool itself --------------------------------------------------------------------------------------

def test_benching_grows_with_each_strike_and_is_forgiven(models):
    t, spans = 1_000_000.0, []
    for _ in range(7):                                      # each strike soon after the last bench ends
        span = live_models.strike("models/a-live", "stall", now=t)
        spans.append(span)
        t += min(span, 3600) + 60                           # (within a day of the last strike)
        if span >= 86400:
            t = t - min(span, 3600) - 60 + 3600             # still inside the day: strikes keep counting
    assert spans == [300, 1200, 3600, 5 * 3600, 86400, 7 * 86400, 7 * 86400]   # 5 min ... a week, then a week
    live_models.reset_for_tests(live_models.STATE.with_name("fresh.json"))
    t = 2_000_000.0
    assert live_models.strike("models/a-live", "x", now=t) == 300
    assert live_models.strike("models/a-live", "x", now=t + 10) == 290          # the same trouble twice: one strike
    assert live_models.strike("models/a-live", "x", now=t + 400) == 1200        # again soon after: longer
    later = t + 400 + live_models.FORGIVE_AFTER + 10
    assert live_models.strike("models/a-live", "x", now=later) == 300           # a clean day: forgiven


def test_the_ladder():
    assert live_models.BENCH == (300, 1200, 3600, 5 * 3600, 86400, 7 * 86400)


def test_choose_the_best_ready_model_and_come_back(models):
    now = 5_000_000.0
    assert live_models.choose("models/a-live", now=now) == "models/a-live"
    live_models.strike("models/a-live", "stall", now=now)
    assert live_models.choose("models/a-live", now=now + 1) == "models/b-live"
    live_models.strike("models/b-live", "stall", now=now + 2)
    assert live_models.choose("models/a-live", now=now + 3) == "models/c-live"
    live_models.strike("models/c-live", "stall", now=now + 4)
    assert live_models.choose("models/a-live", now=now + 5) == "models/a-live"   # all resting: back soonest
    assert live_models.better("models/c-live", now=now + 301) == "models/a-live"  # a is off the bench


def test_good_answers_forgive_a_strike(models):
    now = time.time()
    live_models.strike("models/a-live", "slow", now=now - 400)                  # bench over, 1 strike left
    for _ in range(live_models.GOOD_TO_FORGIVE):
        live_models.good("models/a-live")
    assert live_models.strike("models/a-live", "slow", now=now) == 300           # first rung again


def test_the_state_survives_a_restart(models):
    live_models.strike("models/b-live", "quota")
    path = live_models.STATE
    live_models.reset_for_tests(path)
    assert live_models.benched("models/b-live") > 0


def test_near_the_token_limit_the_model_rests_without_a_strike(models):
    t = 100.0
    assert not live_models.tokens("models/a-live", 25_000, now=t)
    assert not live_models.tokens("models/a-live", 25_000, now=t + 10)
    assert live_models.tokens("models/a-live", 25_000, now=t + 20)              # 75K in a minute > 85% of 65K
    assert 0 < live_models.benched("models/a-live") <= 60
    assert live_models.strike("models/a-live", "later", now=time.time() + 120) == 300   # no strike was counted
    assert not live_models.tokens("models/b-live", 25_000, now=t + 200)


def test_the_thinking_model_gets_a_thinking_level():
    from google.genai import types
    settings = types.LiveConnectConfig(response_modalities=["AUDIO"])
    live_models.tune(settings, "models/gemini-3.8-live-extended-thinking")
    assert settings.thinking_config.thinking_level == types.ThinkingLevel.LOW
    live_models.tune(settings, "models/gemini-3.8-live")
    assert settings.thinking_config is None


def test_mint_model_goes_first(models, monkeypatch):
    monkeypatch.setenv("MINT_MODEL", "models/c-live")
    assert live_models.pool()[0] == "models/c-live" and len(live_models.pool()) == 3


LISTED = ["models/gemini-3.5-transcribe-live", "models/gemini-2.5-flash-native-audio-latest",
          "models/gemini-3.1-flash-live-preview", "models/gemini-3.8-live", "models/gemini-3.8-live-extended-thinking",
          "models/gemini-robotics-er-2-streaming-preview", "models/gemini-3.5-live-translate-preview"]


def test_rank_keeps_conversation_models_newest_first():
    assert live_models.rank(LISTED) == ["models/gemini-3.8-live", "models/gemini-3.1-flash-live-preview",
                                        "models/gemini-3.8-live-extended-thinking"]   # thinking: unreliable with tools
    assert live_models.rank(LISTED + ["models/gemini-4-live-preview", "models/gemini-4-live"])[:2] == \
        ["models/gemini-4-live", "models/gemini-4-live-preview"]          # a released model before its preview


def test_discover_asks_google_once_a_day_and_feeds_the_pool(models, monkeypatch):
    try:
        from mint.core import gemini_keys
    except ImportError:
        from mint import gemini_keys
    calls = []

    class Model:
        def __init__(self, name):
            self.name, self.supported_actions = name, ["bidiGenerateContent"]

    class Client:
        class models:
            @staticmethod
            def list():
                calls.append(1)
                return [Model(n) for n in LISTED] + [type("M", (), {"name": "models/x-live",
                                                                     "supported_actions": ["generateContent"]})()]
    monkeypatch.setattr(gemini_keys, "client", lambda *a, **k: Client())
    found = live_models.discover()
    assert found[0] == "models/gemini-3.8-live" and "models/x-live" not in found
    assert live_models.pool() == found
    live_models.discover()
    assert len(calls) == 1                                                  # cached for a day
    monkeypatch.setattr(gemini_keys, "client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert live_models.discover(force=True) == found                       # offline: the last list stays


def test_a_model_that_refuses_the_session_is_set_aside(models):
    m = _mint()
    asyncio.run(m._backoff(RuntimeError("1007 None. Thinking level must be specified for this model."), 1))
    assert models.MODEL == "models/b-live" and live_models.benched("models/a-live") > 6 * 86400


def test_any_thinking_model_gets_a_level():
    from google.genai import types
    settings = types.LiveConnectConfig(response_modalities=["AUDIO"])
    live_models.tune(settings, "models/gemini-4-live-thinking")
    assert settings.thinking_config.thinking_level == types.ThinkingLevel.LOW


def _promising(models, monkeypatch, **kw):
    monkeypatch.setattr(session, "PROMISE_WAIT", 0.05)
    m = _mint(_busy=False, _stop_epoch=1, _unanswered="", **kw)
    m.moves = []
    m._move_model = lambda why, strike=True, at_once=False: m.moves.append((why, strike, at_once))
    return m


def test_a_promise_without_a_tool_call_goes_to_the_next_model(models, monkeypatch):
    """6 Oct: the thinking model said "I'll start that research" and never called a tool (2 of 3 runs)."""
    m = _promising(models, monkeypatch)
    asyncio.run(m._promise_watch("research macOS news and save it", time.monotonic(), 1))
    assert m._unanswered == "research macOS news and save it"
    assert m.moves == [("promised an action, made no tool call", False, True)]
    assert live_models.benched("models/a-live") > 0


def test_a_promise_kept_or_stopped_is_left_alone(models, monkeypatch):
    m = _promising(models, monkeypatch)

    async def run():
        task = asyncio.create_task(m._promise_watch("do it", time.monotonic(), 1))
        await asyncio.sleep(0.01)
        m._tool_call_at = time.monotonic()                  # the tool call came
        await task
    asyncio.run(run())
    m2 = _promising(models, monkeypatch)
    asyncio.run(m2._promise_watch("do it", time.monotonic(), 0))   # "stop" since (a new epoch)
    assert m.moves == [] and m2.moves == [] and live_models.benched("models/a-live") == 0


def test_cached_models_are_ranked_again(models, monkeypatch):
    live_models._load()["_found"] = {"at": time.time(), "models": [
        "models/gemini-3.8-live", "models/gemini-3.8-live-extended-thinking", "models/gemini-3.1-flash-live-preview"]}
    assert live_models.pool()[-1] == "models/gemini-3.8-live-extended-thinking"


def test_only_input_tokens_count_toward_the_limit(models, monkeypatch):
    """The quota is input tokens a minute: a turn's prompt_token_count, not its total (which adds the reply)."""
    m = _mint()
    seen = []
    monkeypatch.setattr(live_models, "tokens", lambda model, n, now=None: seen.append(n) or False)
    m._watch_context = lambda meta, steps=1: None

    class Meta:
        prompt_token_count, response_token_count, total_token_count = 24_000, 3_000, 27_000

    class Response:
        usage_metadata = Meta()
        session_resumption_update = go_away = tool_call = server_content = data = None

    class Stop(Exception):
        pass
    try:
        try:
            from mint.core import usage
        except ImportError:
            from mint import usage
        monkeypatch.setattr(usage, "live", lambda *a: None)
        m._note_latency = lambda now: (_ for _ in ()).throw(Stop())
        asyncio.run(m._handle(Response()))
    except Exception:
        pass
    assert seen == [24_000]
