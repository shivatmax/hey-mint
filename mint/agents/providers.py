"""One chat() for every model an agent may use, with tool calling.

Messages are kept in a neutral shape and converted per provider:
    {"role": "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "name", "args"}],   # assistant turns that call tools
     "tool_call_id": str, "name": str}          # tool results
Tools are {"name", "description", "parameters": JSON schema}.

Gemini (google-genai, GEMINI_API_KEY) and OpenAI (Chat Completions over
httpx, OPENAI_API_KEY). Models are tried in order: a model that is out of
quota (429), overloaded (503) or unknown (404) is skipped for five minutes -
on this key several Gemini models answer 429 at any given time.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

log = logging.getLogger("mint.agents")

_dead: dict[tuple, float] = {}       # (provider, model) -> skip until (monotonic time)
BACKOFF = (0, 3, 8, 15)                # seconds before each round through the models


class NoProvider(RuntimeError):
    pass


KEYS = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
BASES = {"openai": ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
         "openrouter": ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")}
EFFORTS = ("none", "low", "medium")      # never high: the user asked for low-to-medium thinking at most


class KeyProblem(RuntimeError):
    """The provider refused the key itself (expired, revoked, wrong) - retrying cannot help."""


# provider -> (the key that failed, why). Cleared by itself when the key in .env changes.
BAD_KEYS: dict[str, tuple[str, str]] = {}


def key_problem(provider: str) -> str:
    bad = BAD_KEYS.get(provider)
    if bad and bad[0] == os.environ.get(KEYS.get(provider, ""), ""):
        return bad[1]
    return ""


def available(provider: str) -> bool:
    return bool(os.environ.get(KEYS.get(provider, "")))


_http = None


def _client():
    """One HTTP client for every agent step: keep-alive saves a TLS handshake
    (100-300 ms) per model call, and agents make many."""
    global _http
    if _http is None:
        import httpx
        _http = httpx.Client(limits=httpx.Limits(max_connections=24, max_keepalive_connections=12))
    return _http


def check_keys() -> dict:
    """Ask each configured provider whether it accepts its key (cheap calls, no
    model run), so routing knows up front - otherwise the first task would say
    "via OpenRouter" and only then find the OpenRouter key expired."""
    import httpx
    results = {}
    # Only OpenAI: agents never use OpenRouter (the user's choice), so its key is not even checked.
    probes = {"openai": (os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/models", None)}
    for provider, (url, _) in probes.items():
        key = os.environ.get(KEYS[provider], "")
        if not key:
            continue
        try:
            response = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
        except Exception as error:
            results[provider] = f"unreachable ({str(error)[:60]})"
            continue
        if response.status_code in (401, 403):
            try:
                message = response.json().get("error", {}).get("message", "")
            except Exception:
                message = response.text[:100]
            BAD_KEYS[provider] = (key, f"{provider} refused the API key ({response.status_code}: {message}). "
                                       f"The user must make a new key and put it in .env as {KEYS[provider]}.")
            results[provider] = f"refused: {message}"
        else:
            BAD_KEYS.pop(provider, None)
            results[provider] = "ok" if response.status_code == 200 else f"HTTP {response.status_code}"
    log.info("agent provider keys: %s", results)
    return results


def _direct(models: list[str]) -> list[str]:
    """OpenRouter ids ("openai/gpt-6-luna") as OpenAI's own ids ("gpt-6-luna")."""
    return [m.split("/", 1)[1] if m.startswith("openai/") else m for m in models]


def route(agent: dict) -> tuple[str, list[str], str]:
    """-> (provider, models, note). Falls back when the agent's provider has no key."""
    provider, models = agent.get("provider", "gemini"), list(agent.get("models") or [])
    if provider == "codex":
        from mint.agents import codex
        if codex.problem():
            raise NoProvider(codex.problem())
        return "codex", models, ""
    if provider == "openrouter":
        # Never OpenRouter (the user's rule): the same model, straight from OpenAI.
        provider, models = "openai", _direct(models)
    if provider == "openai":
        if not available("openai"):
            raise NoProvider("Agents run on GPT-6 Luna from OpenAI, and there is no OPENAI_API_KEY in .env.")
        return "openai", models or ["gpt-6-luna"], ""
    if available(provider):
        return provider, models, ""
    fallback = agent.get("fallback") or {"provider": "gemini", "models": []}
    fb_provider = fallback.get("provider", "gemini")
    if available(fb_provider):
        from mint.agents.registry import GEMINI_FAST
        return (fb_provider, list(fallback.get("models") or GEMINI_FAST),
                f"{provider} has no API key yet (add {provider.upper()}_API_KEY to .env); using {fb_provider}")
    raise NoProvider(f"No API key for {provider} or {fb_provider}.")


def _skip(provider, model) -> bool:
    return time.monotonic() < _dead.get((provider, model), 0.0)


def _mark(provider, model, error) -> bool:
    """Bench a failing model for a while. -> True if the failure is transient."""
    text = str(error)
    if any(code in text for code in ("404", "NOT_FOUND", "model_not_found", "does not exist")):
        _dead[(provider, model)] = time.monotonic() + 3600        # not on this key
        return False
    if any(code in text for code in ("429", "RESOURCE_EXHAUSTED")):
        # A daily quota ("...PerDay...") will not come back in five minutes.
        _dead[(provider, model)] = time.monotonic() + (3600 if "PerDay" in text else 300)
        return True
    if any(code in text for code in ("503", "UNAVAILABLE", "500", "INTERNAL", "timed out", "Timeout")):
        # Overloaded - "high demand" blips pass quickly. Benching for five
        # minutes left a whole chain empty in testing and failed two agents.
        _dead[(provider, model)] = time.monotonic() + 30
        return True
    return False


def chat(agent: dict, system: str, messages: list[dict], tools: list[dict], effort: str | None = None) -> dict:
    """-> {"text", "tool_calls", "provider", "model", "note"}. Blocking.

    `effort` is the reasoning effort for models that think: none, low or medium."""
    provider, models, note = route(agent)
    last, transient = None, True
    for round_, wait in enumerate(BACKOFF):
        if round_ and not transient:
            break
        if wait:
            log.info("all %s models busy; retrying in %ss", provider, wait)
            time.sleep(wait)
        transient = False
        for model in models:
            # First round respects the bench; later rounds try every model again.
            if round_ == 0 and _skip(provider, model):
                transient = True
                continue
            try:
                started = time.monotonic()
                if provider == "openai":
                    out = _responses(model, system, messages, tools, effort=effort)
                elif provider == "openrouter":
                    out = _openai(model, system, messages, tools, provider=provider, effort=effort)
                else:
                    out = _gemini(model, system, messages, tools)
                out.update(provider=provider, model=model, note=note,
                           seconds=round(time.monotonic() - started, 1))
                return out
            except KeyProblem:
                if provider == "openrouter" and available("openai"):
                    provider, models, note = route(agent)       # now routes to OpenAI direct
                    return chat(agent, system, messages, tools, effort)
                raise
            except Exception as error:
                last = error
                transient |= _mark(provider, model, error)
                log.info("agent model %s/%s failed: %s", provider, model, str(error)[:160])
    raise RuntimeError(f"no {provider} model answered ({', '.join(models)}): {str(last)[:200]}")


# --- Gemini ------------------------------------------------------------------------

def _gemini(model: str, system: str, messages: list[dict], tools: list[dict]) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                          http_options=types.HttpOptions(timeout=90000,
                                                         retry_options=types.HttpRetryOptions(attempts=1)))
    contents = []
    for m in messages:
        if m["role"] == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m["content"])]))
        elif m["role"] == "assistant":
            if m.get("_gemini") is not None:
                # Gemini 3 wants its own reply back verbatim: the function-call
                # parts carry a thought_signature, and without it the next call
                # fails with 400 INVALID_ARGUMENT.
                contents.append(m["_gemini"])
                continue
            parts = [types.Part(text=m["content"])] if m.get("content") else []
            for call in m.get("tool_calls") or []:
                parts.append(types.Part(function_call=types.FunctionCall(
                    id=call["id"], name=call["name"], args=call["args"])))
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif m["role"] == "tool":
            part = types.Part.from_function_response(name=m["name"], response={"result": m["content"]})
            if part.function_response is not None:
                part.function_response.id = m.get("tool_call_id")
            # Consecutive tool results belong in one turn.
            if contents and contents[-1].role == "user" and contents[-1].parts and \
                    contents[-1].parts[0].function_response is not None:
                contents[-1].parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
    declarations = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                              parameters_json_schema=t["parameters"]) for t in tools]
    reply = client.models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system, temperature=0.4,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    text, calls = [], []
    candidate = (reply.candidates or [None])[0]
    for part in (candidate.content.parts if candidate and candidate.content and candidate.content.parts else []):
        if part.function_call is not None:
            calls.append({"id": part.function_call.id or f"call_{uuid.uuid4().hex[:8]}",
                          "name": part.function_call.name, "args": dict(part.function_call.args or {})})
        elif part.text and not getattr(part, "thought", False):
            text.append(part.text)
    raw = candidate.content if candidate is not None and candidate.content is not None else None
    return {"text": "".join(text).strip(), "tool_calls": calls, "_gemini": raw}


# --- OpenAI --------------------------------------------------------------------------

def _openai(model: str, system: str, messages: list[dict], tools: list[dict],
            provider: str = "openai", effort: str | None = None) -> dict:
    """OpenAI-compatible Chat Completions: OpenAI itself, or OpenRouter."""
    import httpx

    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            entry = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                entry["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                       for c in m["tool_calls"]]
            if m.get("_reasoning_details"):
                # Reasoning models continue their thinking across tool calls only
                # if their reasoning blocks come back exactly as they were sent.
                entry["reasoning_details"] = m["_reasoning_details"]
            out.append(entry)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    body = {"model": model, "messages": out}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["parameters"]}} for t in tools]
    if effort in EFFORTS:
        body["reasoning"] = {"effort": effort}
    env, default = BASES[provider]
    base = os.environ.get(env, default).rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ[KEYS[provider]]}"}
    if provider == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/savka777/jev-use", "X-Title": "Mint"})
    response = _client().post(f"{base}/chat/completions", json=body, timeout=180, headers=headers)
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code in (401, 403):
        message = (data.get("error") or {}).get("message", response.text[:200]) if isinstance(data, dict) else ""
        problem = (f"{provider} refused the API key ({response.status_code}: {message}). The user must make a "
                   f"new key{' at openrouter.ai/keys' if provider == 'openrouter' else ''} and put it in .env as "
                   f"{KEYS[provider]}, then restart Mint.")
        BAD_KEYS[provider] = (os.environ.get(KEYS[provider], ""), problem)
        raise KeyProblem(problem)
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        raise RuntimeError(f"{response.status_code} {str(data.get('error') if isinstance(data, dict) else '')[:300] or response.text[:300]}")
    message = data["choices"][0]["message"]
    calls = []
    for call in message.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": call["id"], "name": call["function"]["name"], "args": args})
    usage = data.get("usage") or {}
    return {"text": (message.get("content") or "").strip(), "tool_calls": calls,
            "_reasoning_details": message.get("reasoning_details"),
            "usage": {"reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                      "cost": usage.get("cost")}}


def _responses(model: str, system: str, messages: list[dict], tools: list[dict], effort: str | None = None) -> dict:
    """OpenAI's Responses API. Chat Completions refuses function tools together
    with reasoning effort for gpt-6-luna ("use /v1/responses or set
    reasoning_effort to 'none'"), so agents use this. Each turn continues from
    the previous response (previous_response_id), which keeps the model's
    reasoning between tool calls without sending it back ourselves."""
    import httpx

    last = max((i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("_response_id")), default=None)
    items = []
    for m in (messages[last + 1:] if last is not None else messages):
        if m["role"] == "user":
            items.append({"role": "user", "content": m["content"]})
        elif m["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        elif m["role"] == "assistant" and m.get("content"):
            items.append({"role": "assistant", "content": m["content"]})
    body = {"model": model, "instructions": system, "input": items}
    if last is not None:
        body["previous_response_id"] = messages[last]["_response_id"]
    if tools:
        body["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]} for t in tools]
    if effort in EFFORTS:
        body["reasoning"] = {"effort": effort}
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    response = _client().post(f"{base}/responses", json=body, timeout=240,
                          headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code in (401, 403):
        message = (data.get("error") or {}).get("message", response.text[:200]) if isinstance(data, dict) else ""
        problem = (f"openai refused the API key ({response.status_code}: {message}). The user must make a new key "
                   "and put it in .env as OPENAI_API_KEY, then restart Mint.")
        BAD_KEYS["openai"] = (os.environ.get("OPENAI_API_KEY", ""), problem)
        raise KeyProblem(problem)
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        raise RuntimeError(f"{response.status_code} {str((data or {}).get('error'))[:300] or response.text[:300]}")
    text, calls = [], []
    for item in data.get("output", []):
        if item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": item["call_id"], "name": item["name"], "args": args})
        elif item.get("type") == "message":
            text += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
    usage = data.get("usage") or {}
    return {"text": "".join(text).strip(), "tool_calls": calls, "_response_id": data.get("id"),
            "usage": {"reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
                      "output_tokens": usage.get("output_tokens")}}


def openai_models() -> list[str]:
    """Model ids the OpenAI key can use (to find the exact 'Luna' id)."""
    import httpx
    response = httpx.get(f"{os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/')}/models",
                         headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=30)
    response.raise_for_status()
    return sorted(m["id"] for m in response.json().get("data", []))
