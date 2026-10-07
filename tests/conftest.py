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
