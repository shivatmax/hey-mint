"""Shared test setup: nothing a test does is written to the real Mint folder."""
import pytest


@pytest.fixture(autouse=True)
def _private_agent_history(tmp_path, monkeypatch):
    """Sessions that finish in a test would be recorded in the user's agent-history.json: keep them in tmp."""
    try:
        from mint.tools import agent_history
    except ImportError:
        try:
            from mint import agent_history
        except ImportError:
            yield
            return
    monkeypatch.setattr(agent_history, "PATH", str(tmp_path / "agent-history.json"))
    monkeypatch.setattr(agent_history, "_items", None)
    yield
    timer = agent_history._timer
    if timer is not None:
        timer.cancel()
        agent_history._timer = None


@pytest.fixture(autouse=True)
def _no_live_codex(monkeypatch):
    """Usage limits: never start the real `codex app-server` from a test (and no reading left from another test)."""
    try:
        from mint.tools import agent_watch
    except ImportError:
        try:
            from mint import agent_watch
        except ImportError:
            yield
            return
    monkeypatch.setattr(agent_watch, "codex_live", lambda *a, **k: None)
    monkeypatch.setattr(agent_watch, "LIVE", {})
    monkeypatch.setattr(agent_watch, "_live", {"wanted": 0.0, "at": -1e9, "busy": False, "failed": -1e9})
    yield


@pytest.fixture(autouse=True)
def _no_background_pointer(monkeypatch):
    """Mint's own pointer posts real events to real windows: off in tests unless a test fakes the window server."""
    try:
        from mint.screen import bg_pointer
    except ImportError:
        try:
            from mint import bg_pointer
        except ImportError:
            yield
            return
    monkeypatch.setattr(bg_pointer, "_off", {"why": "off in tests"})
    monkeypatch.setattr(bg_pointer, "_no_reach", {})
    monkeypatch.setattr(bg_pointer, "_reached", set())
    yield


@pytest.fixture(autouse=True)
def _private_tool_log(tmp_path, monkeypatch):
    """Tool calls run by a test (fake jobs calling web_search "A") went into the user's real
    ~/Library/Logs/Mint/tools.log, mixed in with real requests: keep them in tmp."""
    try:
        from mint.app import session
    except ImportError:
        try:
            from mint import session
        except ImportError:
            yield
            return
    monkeypatch.setattr(session, "TRACE", str(tmp_path / "tools.log"))
    yield
