"""macOS's automation check can hang for good on an app that was just quit (Spotify): Mint gives up and says
"unknown" instead of freezing Settings' apps list and the library, and doesn't ask again while it's stuck."""
import threading
import time

try:
    from mint.tools import connectors
except ImportError:
    from mint import connectors


def test_a_hanging_check_gives_up(monkeypatch):
    release = threading.Event()
    calls = []
    monkeypatch.setattr(connectors, "_automation", lambda b, ask: (calls.append(b), release.wait(5)) and 0)
    monkeypatch.setattr(connectors, "AUTOMATION_WAIT", 0.2)
    monkeypatch.setattr(connectors, "_stuck", set())
    started = time.monotonic()
    assert connectors.automation("com.spotify.client") == connectors.AE_NOT_RUNNING
    assert time.monotonic() - started < 1.0
    assert connectors.automation("com.spotify.client") == connectors.AE_NOT_RUNNING and calls == ["com.spotify.client"]
    release.set()
    time.sleep(0.1)
    assert connectors.automation("com.spotify.client") == 0        # it came back: asked normally again


def test_a_quick_answer_passes_through(monkeypatch):
    monkeypatch.setattr(connectors, "_automation", lambda b, ask: -1743)
    monkeypatch.setattr(connectors, "_stuck", set())
    assert connectors.automation("com.apple.Notes") == -1743
