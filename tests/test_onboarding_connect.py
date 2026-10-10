"""The welcome window's Connect page: the Gemini key is checked with Google before it is saved, a key Google
refused is never saved, and the app starts without a key (the session waits for it)."""
import io
import json
import urllib.error

import pytest

try:
    from mint.ui import onboarding
except ImportError:
    from mint import onboarding


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError("https://example.com", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("answer,verdict", [
    (_Reply(b'{"models": []}'), "ok"),
    (_http_error(400, {"error": {"message": "API key not valid. Please pass a valid API key.",
                                 "details": [{"reason": "API_KEY_INVALID"}]}}), "bad"),
    (_http_error(403, {"error": {"message": "Generative Language API has not been used in project"}}), "bad"),
    (_http_error(429, {"error": {"message": "Resource exhausted"}}), "ok"),
    (urllib.error.URLError("offline"), "offline"),
])
def test_the_key_is_checked_with_google(monkeypatch, answer, verdict):
    seen = {}

    def urlopen(request, timeout=0):
        seen["url"], seen["header"] = request.full_url, request.get_header("X-goog-api-key")
        if isinstance(answer, Exception):
            raise answer
        return answer
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    got, words = onboarding._verify_key("AIza-test-key-0000000000000000")
    assert got == verdict
    assert "AIza" not in seen["url"] and seen["header"] == "AIza-test-key-0000000000000000"   # header, never the URL
    if verdict == "bad":
        assert words


def test_connect_is_a_page_and_the_tour_fits():
    assert onboarding.PAGES.index("connect") == onboarding.PAGES.index("about") + 1
    # The feature list scrolls (8 Oct: 25 features), so it no longer has to fit the page; it must not be thin.
    assert len(onboarding.TOUR) >= 10
    titles = {t[2] for t in onboarding.TOUR}
    assert {"Telegram, from your phone", "Google Meet with me", "Email, handled", "Downloads from YouTube",
            "A team of agents", "Asks before deleting"} <= titles
    for symbol, rgb, title, text, say, clip in onboarding.TOUR:      # 8 Oct: each feature plays its own clip
        assert symbol and len(rgb) == 3 and title and text and say and clip
        assert "Priya" not in repr((title, text, say, clip))


def test_a_refused_key_is_not_saved_when_leaving(monkeypatch):
    saved = []
    from mint.agents import catalog
    monkeypatch.setattr(catalog, "write_key", lambda env, value: saved.append(value))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    class Field:
        def __init__(self, text):
            self.text = text

        def stringValue(self):
            return self.text
    o = onboarding.Onboarding()
    o.page, o.view = onboarding.PAGES.index("connect"), object()
    o.fields = {"gemini_key": Field("AIza-refused-key-000000000000000")}
    o._bad_key = "AIza-refused-key-000000000000000"
    o._leave()
    assert saved == []
    o.fields = {"gemini_key": Field("AIza-unchecked-key-0000000000000")}
    o._leave()
    assert saved == ["AIza-unchecked-key-0000000000000"]


@pytest.mark.parametrize("answer,verdict", [
    (_Reply(b'{"answers": {}}'), "ok"),
    (_http_error(401, {"error": "unauthorized"}), "bad"),
    (_http_error(403, {"error": "forbidden"}), "bad"),
    (_http_error(429, {"error": "busy"}), "ok"),
    (urllib.error.URLError("offline"), "offline"),
])
def test_the_optional_jev_key_is_checked_with_typesafe(monkeypatch, answer, verdict):
    seen = {}

    def urlopen(request, timeout=0):
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        if isinstance(answer, Exception):
            raise answer
        return answer
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    got, words = onboarding._verify_jev_key("ts-test-key-000000000000")
    assert got == verdict
    assert "ts-test" not in seen["url"] and seen["auth"] == "Bearer ts-test-key-000000000000"
    if verdict == "bad":
        assert words


def test_jev_is_an_optional_page_after_gemini():
    assert onboarding.PAGES.index("jev") == onboarding.PAGES.index("connect") + 1
    assert onboarding.TYPESAFE_KEYS == "https://console.typesafe.ai/keys"
    assert onboarding.JEV_ENV == "TYPESAFE_API_KEY"


def test_reopening_shows_one_page_not_a_pile(monkeypatch, tmp_path):
    """Closing setup (or "What I can do") and opening the walkthrough again left the old page on the stage: the
    welcome, About you and the tour drew on top of each other."""
    import AppKit
    prefs = onboarding.prefs
    monkeypatch.setattr(prefs, "PATH", tmp_path / "settings.json")
    monkeypatch.setattr(prefs, "_values", {})
    monkeypatch.setattr(prefs, "_mtime", None)
    monkeypatch.setattr(prefs, "_listeners", [])
    AppKit.NSApplication.sharedApplication()
    o = onboarding.Onboarding()
    monkeypatch.setattr(o, "_drift", lambda page, animated=True: None)
    o.show(onboarding.PAGES.index("welcome"))
    o.closed()                                              # the red button
    o.show(onboarding.PAGES.index("tour"), tour_only=True)  # Settings ▸ the walkthrough
    assert len(o.stage.subviews()) == 1
    o._close_tour()
    o.show(onboarding.PAGES.index("about"))
    o.finish()
    o.show(onboarding.PAGES.index("tour"), tour_only=True)
    o._go(onboarding.PAGES.index("tour"))
    assert len(o.stage.subviews()) == 1 and o.view is o.stage.subviews()[0]
    o._leave()
    o.window.orderOut_(None)
