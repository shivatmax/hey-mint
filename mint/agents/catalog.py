"""The model providers the agents can use, their models, and the user's choices about them.

Every model is written "provider/model", as LiteLLM does: "openai/gpt-6-luna",
"anthropic/claude-sonnet-5-5", "gemini/gemini-3.5-flash", "groq/openai/gpt-oss-120b",
"openrouter/openai/gpt-6-luna", "xai/grok-4.7", "ollama/llama3.2:3b", "<custom id>/<model>".

Keys live in .env (mode 600), like the Gemini key. Everything else the user sets in Settings ▸
Models & agents - custom OpenAI-compatible endpoints, the Ollama address, the backup models every agent
falls back to, and whether Gemini is the last resort - is in providers.json next to agents.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.agents")

PATH = config.PROJECT_ROOT / "providers.json"

# kind: how to talk to it. "responses" = OpenAI Responses API; "chat" = OpenAI-compatible Chat Completions;
# "anthropic" = the Anthropic SDK; "gemini" = google-genai.
PROVIDERS: dict[str, dict] = {
    "openai": {"label": "OpenAI", "kind": "responses", "env": "OPENAI_API_KEY",
               "base": "https://api.openai.com/v1", "base_env": "OPENAI_BASE_URL",
               "hint": "GPT-6 Luna / Sol, GPT-5.6 Luna / Sol / Terra - platform.openai.com/api-keys",
               "models": ["gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-sol",
                          "gpt-5.6-terra"]},
    "anthropic": {"label": "Anthropic (Claude)", "kind": "anthropic", "env": "ANTHROPIC_API_KEY",
                  "base": "https://api.anthropic.com", "base_env": "ANTHROPIC_BASE_URL",
                  "hint": "Claude Opus 5.5, Sonnet 5.5, Haiku 4.5 - console.anthropic.com",
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1",
                             "claude-sonnet-5", "claude-opus-5"]},
    "gemini": {"label": "Google Gemini", "kind": "gemini", "env": "GEMINI_API_KEY",
               "hint": "The same key as Mint's voice - aistudio.google.com/apikey",
               "models": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                          "gemini-3.1-pro-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]},
    "openrouter": {"label": "OpenRouter", "kind": "chat", "env": "OPENROUTER_API_KEY",
                   "base": "https://openrouter.ai/api/v1", "base_env": "OPENROUTER_BASE_URL",
                   "hint": "Hundreds of models behind one key - openrouter.ai/keys",
                   "models": ["openai/gpt-6-luna", "anthropic/claude-sonnet-5-5", "google/gemini-3.5-flash",
                              "x-ai/grok-4.7", "meta-llama/llama-3.3-70b-instruct"]},
    "groq": {"label": "Groq", "kind": "chat", "env": "GROQ_API_KEY",
             "base": "https://api.groq.com/openai/v1", "base_env": "GROQ_BASE_URL",
             "hint": "Very fast open models, free tier - console.groq.com/keys",
             "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile",
                        "llama-3.1-8b-instant", "qwen/qwen3.8-27b"]},
    "xai": {"label": "xAI Grok", "kind": "chat", "env": "XAI_API_KEY",
            "base": "https://api.x.ai/v1", "base_env": "XAI_BASE_URL",
            "hint": "Grok 4.7 - console.x.ai",
            "models": ["grok-4.7", "grok-4.6", "grok-4.5", "grok-4.20-0309-reasoning",
                       "grok-4.20-0309-non-reasoning"]},
    "ollama": {"label": "Ollama (on this Mac)", "kind": "chat", "env": "",
               "base": "http://localhost:11434/v1", "base_env": "OLLAMA_BASE_URL",
               "hint": "Local models, no key - ollama.com; run `ollama pull llama3.2`",
               "models": []},
}

EFFORT_STYLE = {"openrouter": "reasoning", "groq": "reasoning_effort", "xai": "", "ollama": ""}


# --- the user's settings (providers.json) ------------------------------------------------------

_lock = threading.Lock()
DEFAULT_SETTINGS = {"custom": [], "ollama_base": "", "fallbacks": [], "gemini_last_resort": True}


def settings() -> dict:
    try:
        data = json.loads(PATH.read_text())
        if isinstance(data, dict):
            return {**DEFAULT_SETTINGS, **data}
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_SETTINGS)


def save_settings(data: dict) -> None:
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({**settings(), **data}, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(PATH)
    _models_cache.clear()


def custom_providers() -> dict[str, dict]:
    """User-added OpenAI-compatible endpoints: id -> spec (like PROVIDERS)."""
    out = {}
    for item in settings().get("custom", []):
        if not isinstance(item, dict) or not item.get("id") or not item.get("base_url"):
            continue
        cid = slug(item["id"])
        out[cid] = {"label": item.get("label") or item["id"], "kind": "chat", "env": item.get("key_env", ""),
                    "base": item["base_url"].rstrip("/"), "base_env": "", "custom": True,
                    "hint": item["base_url"], "models": list(item.get("models") or [])}
    return out


def all_providers() -> dict[str, dict]:
    return {**PROVIDERS, **custom_providers()}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "custom"


def add_custom(label: str, base_url: str, key: str = "", models: list[str] | None = None) -> str:
    """Add (or replace) a custom OpenAI-compatible endpoint. Returns its id."""
    cid = slug(label)
    if cid in PROVIDERS:
        cid += "-custom"
    env = f"CUSTOM_{re.sub(r'[^A-Z0-9]', '_', cid.upper())}_API_KEY" if key else ""
    if key:
        write_key(env, key)
    items = [c for c in settings().get("custom", []) if slug(c.get("id", "")) != cid]
    items.append({"id": cid, "label": label, "base_url": base_url.rstrip("/"), "key_env": env,
                  "models": list(models or [])})
    save_settings({"custom": items})
    return cid


def remove_custom(cid: str) -> None:
    items = settings().get("custom", [])
    gone = [c for c in items if slug(c.get("id", "")) == cid]
    save_settings({"custom": [c for c in items if slug(c.get("id", "")) != cid]})
    for item in gone:
        if item.get("key_env"):
            write_key(item["key_env"], "")


def write_key(env: str, value: str) -> None:
    """Into .env (mode 600) and this process's environment; empty removes it."""
    _write_env(env, value)
    if value:
        os.environ[env] = value
    else:
        os.environ.pop(env, None)
    _models_cache.clear()
    try:
        from mint.agents import providers
        providers.BAD_KEYS.pop(provider_for_env(env), None)
        providers.NO_CREDIT.pop(provider_for_env(env), None)
    except Exception:
        pass


ENV_FILE = config.PROJECT_ROOT / ".env"


def _write_env(name: str, value: str) -> None:
    """Set (or, empty, remove) NAME=value in Mint's .env, keeping every other line; mode 600."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError(f"bad key name {name!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("a key cannot contain a line break")
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    kept = [line for line in lines if line.strip().removeprefix("export ").split("=", 1)[0].strip() != name]
    if value:
        kept.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(kept) + "\n")
    os.chmod(ENV_FILE, 0o600)


def provider_for_env(env: str) -> str:
    return next((p for p, spec in all_providers().items() if spec.get("env") == env), "")


def base_url(provider: str) -> str:
    spec = all_providers()[provider]
    if provider == "ollama":
        return (settings().get("ollama_base") or os.environ.get("OLLAMA_BASE_URL") or spec["base"]).rstrip("/")
    return (os.environ.get(spec.get("base_env") or "", "") or spec.get("base", "")).rstrip("/")


def key(provider: str) -> str:
    env = all_providers().get(provider, {}).get("env", "")
    if provider == "gemini":
        from mint.core import gemini_keys
        have = gemini_keys.keys()
        return have[0][1] if have else ""
    return os.environ.get(env, "").strip() if env else ""


def configured(provider: str) -> bool:
    """Has what it needs to be tried: a key, or (Ollama, keyless custom) an address."""
    spec = all_providers().get(provider)
    if spec is None:
        return False
    if provider == "codex":
        return True
    if not spec.get("env"):
        return provider == "ollama" or bool(spec.get("custom"))
    return bool(key(provider))


# --- "provider/model" strings -------------------------------------------------------------------

def split(ref: str, default_provider: str = "") -> tuple[str, str]:
    """"groq/openai/gpt-oss-120b" -> ("groq", "openai/gpt-oss-120b"). A bare model takes default_provider
    (or is guessed from its name)."""
    ref = str(ref or "").strip()
    head, _, rest = ref.partition("/")
    if rest and (head in all_providers() or head == "codex"):
        return head, rest
    if default_provider:
        return default_provider, ref
    return guess_provider(ref), ref


def guess_provider(model: str) -> str:
    low = model.lower()
    if low.startswith("claude"):
        return "anthropic"
    if low.startswith("gemini"):
        return "gemini"
    if low.startswith("grok"):
        return "xai"
    if low.startswith(("gpt", "o1", "o3", "o4", "o5")):
        return "openai"
    if ":" in low:
        return "ollama"
    return "openai"


def join(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def label(ref: str) -> str:
    provider, model = split(ref)
    return f"{model} ({all_providers().get(provider, {}).get('label', provider)})"


_ALIASES = [
    (r"\bopus\b", "anthropic/claude-opus-5-5"), (r"\bsonnet\b", "anthropic/claude-sonnet-5-5"),
    (r"\bhaiku\b", "anthropic/claude-haiku-4-5"), (r"\bfable\b", "anthropic/claude-fable-5-1"),
    (r"\bclaude\b", "anthropic/claude-sonnet-5-5"),
    (r"gpt[\s-]*6\.1[\s-]*sol", "openai/gpt-6.1-sol"), (r"gpt[\s-]*6[\s-]*sol", "openai/gpt-6-sol"),
    (r"gpt[\s-]*6[\s-]*astra", "openai/gpt-6-astra"), (r"gpt[\s-]*5\.6[\s-]*terra", "openai/gpt-5.6-terra"),
    (r"gpt[\s-]*5\.6[\s-]*sol", "openai/gpt-5.6-sol"), (r"gpt[\s-]*5\.6[\s-]*luna", "openai/gpt-5.6-luna"),
    (r"gpt[\s-]*6[\s-]*luna|\bluna\b", "openai/gpt-6-luna"), (r"\bterra\b", "openai/gpt-5.6-terra"),
    (r"\bsol\b", "openai/gpt-6-sol"),
    (r"\bgrok\b", "xai/grok-4.7"), (r"gpt[\s-]*oss", "groq/openai/gpt-oss-120b"),
    (r"\bgroq\b", "groq/openai/gpt-oss-120b"), (r"\bllama\b", "groq/llama-3.3-70b-versatile"),
    (r"gemini.*pro", "gemini/gemini-3.1-pro-preview"), (r"gemini.*lite", "gemini/gemini-3.5-flash-lite"),
    (r"\bgemini\b", "gemini/gemini-3.8-flash"),
]


def resolve(text: str) -> str:
    """Words ("Claude Sonnet", "GPT-6 Sol", "grok", "groq/llama-3.1-8b-instant") -> "provider/model", or ''."""
    text = str(text or "").strip()
    if not text:
        return ""
    head = text.split("/", 1)[0]
    if "/" in text and (head in all_providers() or head == "codex"):
        return text
    low = text.lower()
    if "ollama" in low:
        rest = re.sub(r"\bon\b|\bollama\b|\busing\b|\bvia\b", " ", low).strip()
        local = models("ollama")
        pick = next((m for m in local if rest and rest.split()[0] in m), local[0] if local else "")
        return join("ollama", pick or rest or "llama3.2")
    for spec_id, spec in all_providers().items():
        for model in spec.get("models", []):
            if low == model.lower():
                return join(spec_id, model)
    for pattern, ref in _ALIASES:
        if re.search(pattern, low):
            return ref
    return ""


# --- which models each provider has ------------------------------------------------------------

_models_cache: dict[str, tuple[float, list[str]]] = {}
CACHE_SECONDS = 600


def models(provider: str, refresh: bool = False) -> list[str]:
    """Models to offer for `provider`: the live list when its key works (cached 10 min), suggested first;
    else the suggested list."""
    spec = all_providers().get(provider)
    if spec is None:
        return []
    suggested = list(spec.get("models", []))
    cached = _models_cache.get(provider)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    live: list[str] = []
    if configured(provider):
        try:
            live = _fetch(provider)
        except Exception as error:
            log.info("listing %s models failed: %s", provider, str(error)[:160])
    ordered = [m for m in suggested if not live or m in live] + sorted(m for m in live if m not in suggested)
    result = ordered or suggested
    _models_cache[provider] = (time.monotonic(), result)
    return result


def cached_models(provider: str, limit: int = 30) -> list[str]:
    """For menus built on the main thread: the last live list if there is one, else the suggested
    models - never a network call. refresh_lists() fills the cache in the background."""
    spec = all_providers().get(provider, {})
    cached = _models_cache.get(provider)
    names = cached[1] if cached else list(spec.get("models", []))
    return names[:limit]


def refresh_lists(done=None) -> None:
    """Fetch every configured provider's model list in the background (for the next menus)."""
    def run():
        for provider in all_providers():
            if configured(provider) and provider != "codex":
                models(provider)
        if done:
            done()
    threading.Thread(target=run, daemon=True, name="model-lists").start()


def _fetch(provider: str) -> list[str]:
    import httpx
    spec = all_providers()[provider]
    if spec["kind"] == "anthropic":
        from mint.agents.anthropic_provider import list_models
        return list_models()
    if spec["kind"] == "gemini":
        from mint.core import gemini_keys
        client = gemini_keys.client()
        out = []
        for m in client.models.list():
            name = str(getattr(m, "name", "")).removeprefix("models/")
            actions = [str(a) for a in (getattr(m, "supported_actions", None) or [])]
            if name.startswith("gemini") and (not actions or "generateContent" in actions) and not any(
                    w in name for w in ("live", "tts", "embedding", "image", "audio", "native")):
                out.append(name)
        return out
    if provider == "ollama":
        root = base_url("ollama").removesuffix("/v1")
        data = httpx.get(f"{root}/api/tags", timeout=4).json()
        return [m["name"] for m in data.get("models", [])]
    headers = {"Authorization": f"Bearer {key(provider)}"} if key(provider) else {}
    if provider == "openrouter":
        # Its model list is public, so it cannot tell an expired key: ask about the key itself.
        check = httpx.get(f"{base_url(provider)}/key", headers=headers, timeout=15)
        if check.status_code in (401, 403):
            raise RuntimeError(f"{check.status_code} key refused: {check.text[:120]}")
    response = httpx.get(f"{base_url(provider)}/models", headers=headers, timeout=15)
    if response.status_code in (401, 403):
        raise RuntimeError(f"{response.status_code} key refused")
    response.raise_for_status()
    ids = [str(m.get("id")) for m in response.json().get("data", []) if m.get("id")]
    if provider == "openai":
        # Chat models only: no embeddings, audio, image, moderation or realtime ids.
        ids = [i for i in ids if re.match(r"^(gpt|o\d|chatgpt)", i) and not re.search(
            r"audio|realtime|tts|transcribe|image|embedding|moderation|search|instruct|codex", i)]
    return ids


def test(provider: str) -> str:
    """For Settings' Test button: 'OK - N models' or what is wrong, in plain words."""
    spec = all_providers().get(provider)
    if spec is None:
        return "Unknown provider."
    if not configured(provider):
        return "No key yet." if spec.get("env") else "Not set up."
    try:
        found = _fetch(provider)
    except Exception as error:
        text = str(error)
        unreachable = "connect" in type(error).__name__.lower() or "connection refused" in text.lower() \
            or "timed out" in text.lower() or "nodename" in text.lower()
        if unreachable and provider == "ollama":
            return f"Ollama is not running at {base_url('ollama')} - open the Ollama app, or `ollama serve`."
        if unreachable:
            return f"Could not reach {base_url(provider) or spec['label']} - check the address and the network."
        if "401" in text or "403" in text or "refused" in text or "invalid" in text.lower() \
                or "authentication" in text.lower():
            return "The key was refused - check it, or make a new one."
        return f"Could not reach it: {text[:120]}"
    _models_cache[provider] = (time.monotonic(), found or spec.get("models", []))
    if provider == "ollama" and not found:
        return "Ollama is running but has no models - run `ollama pull llama3.2`."
    return f"OK - {len(found)} model{'s' if len(found) != 1 else ''} available."


# --- the backup chain every agent uses -------------------------------------------------------------

# Last resort for every agent (Settings: "Gemini as the last resort"): the Flash chain, then older Flash models.
GEMINI_LAST = ["gemini/gemini-3.8-flash", "gemini/gemini-3.7-flash", "gemini/gemini-3.6-flash",
               "gemini/gemini-3.5-flash", "gemini/gemini-3.5-flash-lite"]


def fallbacks() -> list[str]:
    """Backup models tried after an agent's own list, in order (Settings ▸ Models & agents)."""
    data = settings()
    chain = [str(m) for m in data.get("fallbacks", []) if str(m).strip()]
    if data.get("gemini_last_resort", True):
        chain += [m for m in GEMINI_LAST if m not in chain]
    return chain
