"""One or two Gemini API keys, each the other's backup.

GEMINI_API_KEY is required; GEMINI_API_KEY_2 is optional (Settings ▸ Models & agents). With two keys the
free-tier limits are per key, so:

  - "split" (the default): the Live voice session uses key 1, every other Gemini call (memory, chat
    summaries, pointing at the screen, agents) uses key 2 - the voice never waits behind background work.
  - "primary": key 1 for everything; key 2 only when key 1 is rate-limited or refused.

Either way, when a key answers 429 / RESOURCE_EXHAUSTED for a model, or is refused, that key is set aside
for a while and the same call is retried at once on the other key. `client()` returns a stand-in for
`genai.Client` that does this inside `models.generate_content`, so call sites keep their code.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("mint.core.gemini_keys")

ENVS = ("GEMINI_API_KEY", "GEMINI_API_KEY_2")
_bench: dict[tuple[str, str], tuple[float, str]] = {}      # (env, model or "*") -> (until, why)
_lock = threading.Lock()
_clients: dict[tuple, object] = {}


def keys() -> list[tuple[str, str]]:
    """[(env name, key)] of the keys that are set, key 1 first."""
    return [(env, os.environ[env].strip()) for env in ENVS if os.environ.get(env, "").strip()]


def mode() -> str:
    try:
        from mint.core import prefs
        value = prefs.get("gemini_key_mode")
    except Exception:
        value = None
    return value if value in ("split", "primary") else "split"


def _order(use: str) -> list[tuple[str, str]]:
    have = keys()
    if len(have) > 1 and mode() == "split" and use != "live":
        have = have[1:] + have[:1]
    return have


def _benched(env: str, model: str) -> float:
    now = time.monotonic()
    until = max(_bench.get((env, model), (0.0, ""))[0], _bench.get((env, "*"), (0.0, ""))[0])
    return until if until > now else 0.0


def pick(use: str = "llm", model: str = "*") -> tuple[str, str]:
    """(env, key) to use now: the preferred key for `use` unless it is set aside; else the one free
    soonest. Raises KeyError when no key is set."""
    order = _order(use)
    if not order:
        raise KeyError("GEMINI_API_KEY")
    with _lock:
        for env, key in order:
            if not _benched(env, model):
                return env, key
        return min(order, key=lambda item: _benched(item[0], model))


def bench(env: str, seconds: float, why: str = "", model: str = "*") -> None:
    with _lock:
        _bench[(env, model)] = (time.monotonic() + seconds, why)
    log.info("gemini key %s set aside for %ss (%s) %s", env, int(seconds), model, why[:120])


def status() -> dict[str, str]:
    """For Settings: env -> '' or why it is set aside right now."""
    out = {}
    for env, _ in keys():
        whys = [why for (e, m), (until, why) in _bench.items() if e == env and until > time.monotonic()]
        out[env] = whys[0] if whys else ""
    return out


def classify(error) -> str:
    """'quota' (429 / out of quota - try the other key), 'key' (refused - set it aside long), or ''."""
    text = str(error)
    lowered = text.lower()
    if "429" in text or "resource_exhausted" in lowered or "quota" in lowered or "rate limit" in lowered:
        return "quota"
    if "api_key_invalid" in lowered or "api key not valid" in lowered or "permission_denied" in lowered \
            or "suspended" in lowered or " 401" in text or " 403" in text or "api key expired" in lowered:
        return "key"
    return ""


def failed(env: str, error, model: str = "*") -> bool:
    """Record a failed call; True if another key is worth trying for it."""
    kind = classify(error)
    if not kind or len(keys()) < 2:
        return False
    if kind == "key":
        bench(env, 6 * 3600, str(error)[:160])
    else:
        bench(env, 3600 if "PerDay" in str(error) else 90, str(error)[:160], model)
    # Only worth it if another key is free for this model right now (else the caller's own backoff runs).
    return any(other != env and not _benched(other, model) for other, _ in keys())


def _real(key: str, options: dict):
    from google import genai
    cache_key = (key, repr(sorted(options.items())))
    with _lock:
        client = _clients.get(cache_key)
        if client is None:
            client = genai.Client(api_key=key, **options)
            _clients[cache_key] = client
        return client


class _Models:
    def __init__(self, owner: "Client") -> None:
        self._owner = owner

    def _call(self, name: str, *args, **kwargs):
        model = str(kwargs.get("model") or (args[0] if args else "*"))
        tried = set()
        while True:
            env, key = pick(self._owner.use, model)
            tried.add(env)
            try:
                return getattr(_real(key, self._owner.options).models, name)(*args, **kwargs)
            except Exception as error:
                if failed(env, error, model) and len(tried) < len(keys()):
                    log.info("gemini %s on %s failed (%s); trying the other key", model, env, str(error)[:80])
                    continue
                raise

    def generate_content(self, *args, **kwargs):
        return self._call("generate_content", *args, **kwargs)

    def generate_content_stream(self, *args, **kwargs):
        return self._call("generate_content_stream", *args, **kwargs)

    def count_tokens(self, *args, **kwargs):
        return self._call("count_tokens", *args, **kwargs)

    def __getattr__(self, name):
        env, key = pick(self._owner.use)
        return getattr(_real(key, self._owner.options).models, name)


class Client:
    """Stands in for google.genai.Client: `models.generate_content` fails over between the keys; anything
    else goes to the client of the key in use now."""

    def __init__(self, use: str = "llm", **options) -> None:
        self.use, self.options = use, options
        self.models = _Models(self)

    def __getattr__(self, name):
        env, key = pick(self.use)
        return getattr(_real(key, self.options), name)


def client(use: str = "llm", **options) -> Client:
    return Client(use, **options)


def live_key() -> tuple[str, str]:
    """The key for the next Live voice connection."""
    return pick("live", "live")
