"""An update the user asked for (voice "update yourself", Settings' Install button) downloads at once and
installs by itself at the first moment Mint is idle - never mid-conversation or mid-task, and never twice.
A failed download backs off and tries again without installing anything. Nothing here touches the network
or the real app: the download, the install and the session are fakes."""
import time
import types
from pathlib import Path

import pytest

try:
    from mint.app import updater
except ImportError:
    from mint import updater


OFFER = {"version": "9.9.9", "tag": "v9.9.9", "dmg": "Hey-Mint-9.9.9-arm64.dmg", "url": "https://example.invalid/x.dmg",
         "size": 50_000_000, "sha256": "0" * 64, "sources": ["latest.json"], "notes": "", "min_macos": "",
         "page": "https://example.invalid", "prerelease": False}


def _session(**changes):
    """A resting session: asleep, nothing running."""
    s = types.SimpleNamespace(asleep=True, paused=False, hands_free=True, _last_active=0.0, _last_voice=0.0,
                              _busy=False, _tool_task=None, _turn_open=False, audio=types.SimpleNamespace(playing=False),
                              _pending_text=None, _pending_wake=None, task=None, _timers=set(), enroller=None,
                              _recalibrating=False, meet=None)
    for key, value in changes.items():
        setattr(s, key, value)
    return s


@pytest.fixture
def up(tmp_path, monkeypatch):
    """The updater with its state in tmp, a packaged app, a release on offer, and fake download / install."""
    monkeypatch.setattr(updater, "STATE_FILE", tmp_path / "update.json")
    monkeypatch.setattr(updater, "CACHE", tmp_path / "cache")
    app = tmp_path / "Hey Mint.app"
    monkeypatch.setattr(updater, "running_app", lambda: app)
    monkeypatch.setattr(updater, "packaged", lambda a: (True, ""))
    monkeypatch.setattr(updater, "current_version", lambda a=None: "1.0.0")
    for name, value in (("_requested", 0.0), ("_offer", dict(OFFER)), ("_staged", None), ("_failures", 0),
                        ("_next_try", 0.0), ("_installing", False), ("_waiting", ""), ("_waiter", None),
                        ("_state", {"status": "idle", "detail": "", "progress": 0.0})):
        monkeypatch.setattr(updater, name, value)
    monkeypatch.setattr(updater, "_ensure_waiter", lambda: None)      # the test drives _tick itself
    monkeypatch.setattr(updater, "_others_busy", lambda: "")
    sess = _session()
    monkeypatch.setattr(updater, "_live_session", lambda: sess)
    calls = types.SimpleNamespace(prepare=0, install=0, fail_prepare=0, session=sess, app=app)

    def prepare(offer, app):
        calls.prepare += 1
        if calls.fail_prepare:
            calls.fail_prepare -= 1
            raise updater.UpdateError("the download stopped at 10 of 50000000 bytes")
        staged = tmp_path / "cache" / offer["version"] / "Hey Mint.app"
        staged.mkdir(parents=True, exist_ok=True)
        updater._staged = staged
        return staged

    def install_now(relaunch=True, quit_after=6.0):
        calls.install += 1
        updater._save(requested=None)
        updater._staged, updater._offer, updater._requested = None, None, 0.0
        return "Updating to Hey Mint 9.9.9."

    monkeypatch.setattr(updater, "prepare", prepare)
    monkeypatch.setattr(updater, "install_now", install_now)
    return calls


def test_asked_while_talking_downloads_now_and_waits(up):
    up.session.asleep = False                       # mid-conversation
    text = updater.request()
    assert updater.requested() and "as soon as Mint is idle" in text
    assert updater._load()["requested"]["version"] == "9.9.9"
    assert updater._tick() == "wait"                # downloaded at once ...
    assert up.prepare == 1 and up.install == 0      # ... but not installed while talking
    assert "now: talking" in updater.info()["detail"] and updater.info()["waiting"] == "talking"
    for _ in range(3):
        assert updater._tick() == "wait"
    assert up.prepare == 1 and up.install == 0


def test_installs_once_as_soon_as_mint_is_asleep(up):
    up.session.asleep = False
    updater.request()
    assert updater._tick() == "wait"
    up.session.asleep = True                        # the conversation ended
    assert updater._tick() == "installed"
    assert up.install == 1 and not updater.requested()
    assert "requested" not in updater._load()
    assert updater._tick() == "done" and up.install == 1


def test_already_idle_installs_at_once(up):
    updater._staged = Path(up.app.parent / "cache" / "9.9.9" / "Hey Mint.app")   # downloaded earlier
    assert updater.request().startswith("Installing Hey Mint 9.9.9")
    assert updater._tick() == "installed" and up.install == 1 and up.prepare == 0


@pytest.mark.parametrize("changes, reason", [
    ({"asleep": False}, "talking"),
    ({"_busy": True}, "running a tool"),
    ({"_tool_task": types.SimpleNamespace(done=lambda: False)}, "running a tool"),
    ({"audio": types.SimpleNamespace(playing=True)}, "speaking"),
    ({"_turn_open": True}, "speaking"),
    ({"task": {"goal": "x", "updated": time.time(), "steps": [{"text": "a", "status": "todo"}]}}, "working on a task"),
    ({"_timers": {object()}}, "a timer is running"),
    ({"enroller": object()}, "training the voice"),
    ({"meet": object()}, "in a Meet call"),
])
def test_each_kind_of_busy_waits(up, monkeypatch, changes, reason):
    if "task" in changes:
        try:
            from mint.app import tasks
        except ImportError:
            from mint import tasks
        monkeypatch.setattr(tasks, "remaining", lambda task: [s for s in task["steps"] if s["status"] == "todo"])
    for key, value in changes.items():
        setattr(up.session, key, value)
    assert updater.session_busy(up.session) == reason
    updater.request()
    assert updater._tick() == "wait" and up.install == 0


def test_paused_counts_as_resting_and_a_stale_task_does_not_block(up):
    up.session.asleep, up.session.paused = False, True
    up.session.task = {"goal": "old", "updated": time.time() - 3600, "steps": []}
    assert updater.session_busy(up.session) == ""


def test_a_meeting_recording_or_dictation_waits(up, monkeypatch):
    monkeypatch.setattr(updater, "_others_busy", lambda: "recording a meeting")
    updater.request()
    assert updater._tick() == "wait" and up.install == 0
    monkeypatch.setattr(updater, "_others_busy", lambda: "")
    assert updater._tick() == "installed" and up.install == 1


def test_sub_agent_running_waits(up, monkeypatch):
    try:
        from mint.agents.runtime import hub
    except ImportError:
        pytest.skip("no agents runtime")
    monkeypatch.setattr(hub, "runs", {"a": types.SimpleNamespace(active=True)})
    assert updater.session_busy(up.session) == "a sub-agent is working"


def test_failed_download_backs_off_and_never_installs(up):
    up.fail_prepare = 2
    updater.request()
    assert updater._tick() == "retry"
    assert up.install == 0 and updater._state["status"] == "error" and "Trying again in 30 s" in updater._state["detail"]
    first = updater._next_try
    assert updater._tick() == "retry" and up.prepare == 1          # not before the backoff is over
    updater._next_try = 0.0
    assert updater._tick() == "retry" and up.prepare == 2 and up.install == 0
    assert updater._next_try - time.time() > first - time.time()   # the wait grows
    assert updater.requested()                                      # still asked for
    updater._next_try = 0.0
    assert updater._tick() == "installed" and up.install == 1 and updater._failures == 0


def test_failed_install_backs_off(up, monkeypatch):
    def broken(relaunch=True, quit_after=6.0):
        raise updater.UpdateError("macOS didn't let Hey Mint replace itself")
    monkeypatch.setattr(updater, "install_now", broken)
    updater.request()
    assert updater._tick() == "retry" and updater.requested() and updater._failures == 1


def test_not_asked_nothing_automatic(up, monkeypatch):
    monkeypatch.setattr(updater, "auto", lambda: False)
    monkeypatch.setattr(updater, "fetch_release", lambda channel="stable": {})
    monkeypatch.setattr(updater, "parse_release", lambda release: dict(OFFER))
    updater._offer = None
    updater.check(now=True)
    assert updater._offer["version"] == "9.9.9" and not updater.requested()
    assert updater._tick() == "done" and up.prepare == 0 and up.install == 0


def test_not_packaged_is_never_updated(up, monkeypatch):
    monkeypatch.setattr(updater, "packaged", lambda a: (False, "Mint is running from source; update it with git."))
    assert "git" in updater.request() and not updater.requested()
    updater._requested = time.time()                # (a request saved by a packaged copy)
    assert updater._tick() == "done" and up.prepare == 0 and up.install == 0 and not updater.requested()


def test_voice_update_says_one_line_and_carries_on(up):
    up.session.asleep = False
    text = updater.tool({"action": "install"})
    assert "Downloading Hey Mint 9.9.9" in text and "I'll install it when I'm idle" in text
    assert updater.requested() and up.install == 0


def test_newest_already_nothing_to_wait_for(up, monkeypatch):
    updater._offer = None
    monkeypatch.setattr(updater, "check", lambda now=False, background=True: (
        updater._set("current", "Hey Mint 1.0.0 is the newest version."), updater._state["detail"])[1])
    assert "newest" in updater.request() and not updater.requested()


def test_a_request_survives_a_reload(up):
    updater._save(requested={"version": "9.9.9", "at": time.time()})
    updater._resume_request()
    assert updater.requested()
    updater._requested = 0.0
    updater._save(requested={"version": "1.0.0", "at": time.time()})    # installed meanwhile
    updater._resume_request()
    assert not updater.requested() and "requested" not in updater._load()


def test_install_runs_once(up, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(updater, "_installing", True)
    called = []
    monkeypatch.setattr(updater, "_install_now", lambda *a: called.append(a))
    assert updater.install_now() == "Already installing the update." and not called
