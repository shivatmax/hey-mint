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


def test_connect_is_a_page_and_the_tour_is_short():
    assert onboarding.PAGES.index("connect") == onboarding.PAGES.index("about") + 1
    assert len(onboarding.TOUR) <= 10
    clips = {t[0] for t in onboarding.TOUR}
    assert {"notch-meet", "notch-agents", "notch-guard"} <= clips


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
