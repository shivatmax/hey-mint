"""Up to five Gemini keys: added one after another, removed with the rest moving up, and the voice moving to the
next key when its key nears Google's per-minute limit (which is per key)."""
try:
    from mint.core import gemini_keys
    from mint.voice import live_models
except ImportError:
    from mint import gemini_keys, live_models


def _clear(monkeypatch):
    for env in gemini_keys.ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(gemini_keys, "_bench", {})


def test_five_keys_in_order(monkeypatch):
    _clear(monkeypatch)
    assert len(gemini_keys.ENVS) == gemini_keys.MAX_KEYS == 5
    assert gemini_keys.next_free() == "GEMINI_API_KEY"
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    assert gemini_keys.next_free() == "GEMINI_API_KEY_3"
    assert [gemini_keys.label(e) for e, _ in gemini_keys.keys()] == ["Key 1", "Key 2"]
    for n in range(3, 6):
        monkeypatch.setenv(f"GEMINI_API_KEY_{n}", f"k{n}")
    assert gemini_keys.next_free() is None and len(gemini_keys.keys()) == 5


def test_removing_a_key_moves_the_rest_up(monkeypatch):
    import os
    _clear(monkeypatch)
    for env, key in zip(gemini_keys.ENVS, ("k1", "k2", "k3")):
        monkeypatch.setenv(env, key)
    monkeypatch.delenv("GEMINI_API_KEY")                       # key 1 removed

    def write(env, value):
        if value:
            os.environ[env] = value
        else:
            os.environ.pop(env, None)
    gemini_keys.compact(write)
    assert [k for _, k in gemini_keys.keys()] == ["k2", "k3"]
    assert os.environ["GEMINI_API_KEY"] == "k2" and "GEMINI_API_KEY_3" not in os.environ


def test_the_voice_moves_to_the_next_key_near_the_limit(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    assert gemini_keys.live_key()[0] == "GEMINI_API_KEY"
    assert gemini_keys.other_free("GEMINI_API_KEY")
    gemini_keys.bench("GEMINI_API_KEY", 60, "near its per-minute token limit", model="live")
    assert gemini_keys.live_key()[0] == "GEMINI_API_KEY_2"
    assert not gemini_keys.other_free("GEMINI_API_KEY_2")       # key 1 is resting: nowhere else to go


def test_per_minute_tokens_are_counted_per_key(monkeypatch):
    monkeypatch.setattr(live_models, "_turns", {})
    rested = []
    monkeypatch.setattr(live_models, "rest", lambda model, *a, **k: rested.append(model))
    t = 1000.0
    assert not live_models.tokens("models/x-live", 30_000, now=t, key="GEMINI_API_KEY", rest_model=False)
    assert not live_models.tokens("models/x-live", 30_000, now=t + 1, key="GEMINI_API_KEY_2", rest_model=False)
    assert live_models.tokens("models/x-live", 30_000, now=t + 2, key="GEMINI_API_KEY", rest_model=False)
    assert rested == []                                          # another key carries on: the model isn't rested
    assert live_models.used("models/x-live", now=t + 3) == 30_000  # (key 1's minute was cleared when it hit)
