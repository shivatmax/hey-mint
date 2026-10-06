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
