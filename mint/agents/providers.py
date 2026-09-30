"""One chat() for every model an agent may use, with tool calling.

Messages are kept in a neutral shape and converted per provider:
    {"role": "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "name", "args"}],   # assistant turns that call tools
     "tool_call_id": str, "name": str}          # tool results
Assistant turns also carry what one provider needs to continue exactly (`_via` = "provider/model" that wrote
it, `_response_id` for OpenAI, `_gemini` for Gemini, `_anthropic` for Claude, `_reasoning_details` for
OpenRouter); another provider gets the neutral form, so a run can move to a backup model mid-way.
Tools are {"name", "description", "parameters": JSON schema}.

Providers (catalog.py): OpenAI (Responses API), Anthropic (SDK, anthropic_provider.py), Gemini (two keys,
gemini_keys.py), and OpenAI-compatible Chat Completions for OpenRouter, Groq, xAI Grok, Ollama and custom
endpoints. An agent has an ordered list of models ("openai/gpt-6-luna", "anthropic/claude-sonnet-5-5", ...),
then the backup models set for every agent (catalog.fallbacks()). A model that is out of quota (429),
overloaded (5xx) or unknown (404) is benched for a while; a refused key or an empty account skips that
provider; the next model in the chain answers instead.
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


KEYS = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY", "xai": "XAI_API_KEY"}
BASES = {"openai": ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
         "openrouter": ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")}


def _key_env(provider: str) -> str:
    from mint.agents import catalog
    return catalog.all_providers().get(provider, {}).get("env") or KEYS.get(provider, "")
EFFORTS = ("none", "low", "medium")      # never high: the user asked for low-to-medium thinking at most


class KeyProblem(RuntimeError):
    """The provider refused the key itself (expired, revoked, wrong) - retrying cannot help."""


# provider -> (the key that failed, why). Cleared by itself when the key in .env changes.
BAD_KEYS: dict[str, tuple[str, str]] = {}
# provider -> (key, why, when noticed): the account has no credits left (OpenAI 429
# insufficient_quota, OpenRouter 402). The key is fine (GET /models still answers 200, so
# check_keys cannot see it), but every model call fails until the user adds credits.
# Found live: two bench agent runs "started", then each crashed ~30 s later after four
# rounds of retries on an error that retrying cannot fix.
NO_CREDIT: dict[str, tuple[str, str, float]] = {}
RECHECK = 60          # seconds before a no-credit account is asked again (the user may have topped up)


def _no_credit(status: int, data) -> bool:
    """Is this error 'the account is out of credits' (not a passing rate limit)?"""
    error = data.get("error") if isinstance(data, dict) else None
    fields = " ".join(str(error.get(k, "")) for k in ("type", "code")) if isinstance(error, dict) else str(error or "")
    return status == 402 or "insufficient_quota" in fields


def _flag_no_credit(provider: str, data) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    message = str(error.get("message", "") if isinstance(error, dict) else error or "")[:160]
    where = {"openai": "platform.openai.com/settings/organization/billing", "openrouter": "openrouter.ai/settings/credits",
             "anthropic": "console.anthropic.com/settings/billing"}.get(provider, f"the {provider} dashboard")
    problem = (f"The {provider} account behind the agents has no credits left ({message or 'insufficient quota'}). "
               f"The user must add credits at {where} (or change the key in Settings ▸ Models & agents).")
    NO_CREDIT[provider] = (os.environ.get(_key_env(provider), ""), problem, time.monotonic())
    log.warning("agents: %s", problem)
    return problem



def _count(model, input_tokens, output_tokens, thinking=0) -> None:
    """Settings ▸ Usage: tokens per model (counts only)."""
    try:
        from mint.core import usage
        usage.record(str(model or "openai"), input_tokens or 0, output_tokens or 0, thinking or 0)
    except Exception:
        pass

def key_problem(provider: str, recheck: bool = False) -> str:
    """Why `provider` cannot run agents now ('' if it can): a refused key, or no credits.

    recheck=True (blocking - not on the event loop): a no-credit account last seen more
    than RECHECK s ago is asked again with a tiny call, so a top-up is noticed at once."""
    key = os.environ.get(_key_env(provider), "") if _key_env(provider) else ""
    bad = BAD_KEYS.get(provider)
    if bad and bad[0] == key:
        return bad[1]
    broke = NO_CREDIT.get(provider)
    if not broke or broke[0] != key:
        NO_CREDIT.pop(provider, None)
        return ""
    if recheck and time.monotonic() - broke[2] > RECHECK:
        NO_CREDIT.pop(provider, None)
        _probe(provider)                     # flags it again if there are still no credits
        broke = NO_CREDIT.get(provider)
    return broke[1] if broke else ""


def _probe(provider: str) -> None:
    """The smallest real model call: does the account have credits? (Flags NO_CREDIT if not.)"""
    if provider not in BASES or not available(provider):
        return
    env, default = BASES[provider]
    base = os.environ.get(env, default).rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ[KEYS[provider]]}"}
    try:
        if provider == "openai":
            response = _client().post(f"{base}/responses", headers=headers, timeout=30, json={
                "model": "gpt-6-luna", "input": "Say ok.", "max_output_tokens": 16, "reasoning": {"effort": "none"}})
        else:
            response = _client().post(f"{base}/chat/completions", headers=headers, timeout=30, json={
                "model": "openai/gpt-6-luna", "messages": [{"role": "user", "content": "Say ok."}], "max_tokens": 16})
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        log.debug("credit probe for %s failed", provider, exc_info=True)
        return                               # unreachable is not "no credits"; the run will say
    if _no_credit(response.status_code, data):
        _flag_no_credit(provider, data)


def available(provider: str) -> bool:
    from mint.agents import catalog
    return catalog.configured(provider)


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
    """Ask each configured provider whether it accepts its key (cheap listing calls, no model run), so
    routing knows up front - otherwise the first task would start on a provider and only then find its
    key refused."""
    from mint.agents import catalog
    results = {}
    for provider, spec in catalog.all_providers().items():
        if provider == "gemini" or not spec.get("env") or not catalog.configured(provider):
            continue
        key = catalog.key(provider)
        try:
            catalog._fetch(provider)
        except Exception as error:
            text = str(error)
            if any(code in text for code in ("401", "403", "refused", "authentication", "permission")):
                BAD_KEYS[provider] = (key, f"{spec['label']} refused the API key ({text[:120]}). The user must make "
                                           "a new key and add it in Settings ▸ Models & agents.")
                results[provider] = "refused"
            else:
                results[provider] = f"unreachable ({text[:60]})"
            continue
        BAD_KEYS.pop(provider, None)
        results[provider] = "ok"
    log.info("agent provider keys: %s", results)
    return results


def _direct(models: list[str]) -> list[str]:
    """OpenRouter ids ("openai/gpt-6-luna") as OpenAI's own ids ("gpt-6-luna")."""
    return [m.split("/", 1)[1] if m.startswith("openai/") else m for m in models]


def chain(agent: dict) -> list[tuple[str, str]]:
    """The models an agent tries, in order: its own, then the backups set for every agent. Deduplicated."""
    from mint.agents import catalog
    if agent.get("provider") == "codex" or agent.get("runner") == "codex":
        return [("codex", m.split("/", 1)[-1]) for m in (agent.get("models") or ["gpt-6-luna"])]
    default = agent.get("provider") or ""
    refs = list(agent.get("models") or [])
    fb = agent.get("fallback")
    if isinstance(fb, dict):                                # the old shape: {"provider", "models"}
        refs += [catalog.join(fb.get("provider", "gemini"), m) for m in fb.get("models") or []]
    if agent.get("use_backups", True):
        refs += catalog.fallbacks()
    out, seen = [], set()
    for ref in refs:
        provider, model = catalog.split(ref, default)
        if (provider, model) not in seen and model:
            seen.add((provider, model))
            out.append((provider, model))
    return out


def _problem(provider: str) -> str:
    from mint.agents import catalog
    if provider == "codex":
        from mint.agents import codex
        return codex.problem()
    spec = catalog.all_providers().get(provider)
    if spec is None:
        return f"'{provider}' is not a provider Mint knows."
    if not catalog.configured(provider):
        return f"{spec['label']} has no API key (add it in Settings ▸ Models & agents)."
    return key_problem(provider)


def route(agent: dict) -> tuple[str, list[str], str]:
    """-> (provider, models, note) of the first model in the agent's chain that can run now. `models` is the
    rest of the chain as "provider/model" (for display); `note` says why earlier ones were skipped."""
    entries = chain(agent)
    skipped = []
    for i, (provider, model) in enumerate(entries):
        problem = _problem(provider)
        if problem:
            skipped.append(f"{provider}/{model}: {problem}")
            continue
        note = ""
        if skipped:
            first = entries[0]
            note = f"{first[0]}/{first[1]} can't run ({skipped[0].split(': ', 1)[1][:100]}); using {provider}/{model}"
        return provider, [model] + [f"{p}/{m}" for p, m in entries[i + 1:]], note
    if not entries:
        raise NoProvider("This agent has no model set (Settings ▸ Models & agents).")
    raise NoProvider("None of this agent's models can run: " + "; ".join(skipped)[:500])


def _skip(provider, model) -> bool:
    return time.monotonic() < _dead.get((provider, model), 0.0)


def _mark(provider, model, error) -> bool:
    """Bench a failing model for a while. -> True if the failure is transient."""
    text = str(error)
    kind = getattr(error, "kind", "")
    if kind == "missing" or any(code in text for code in ("404", "NOT_FOUND", "model_not_found", "does not exist")):
        _dead[(provider, model)] = time.monotonic() + 3600        # not on this key
        return False
    if kind == "quota" or any(code in text for code in ("429", "RESOURCE_EXHAUSTED")):
        # A daily quota ("...PerDay...") will not come back in five minutes.
        _dead[(provider, model)] = time.monotonic() + (3600 if "PerDay" in text else 300)
        return True
    if kind == "busy" or any(code in text for code in ("503", "529", "UNAVAILABLE", "500", "502", "INTERNAL",
                                                       "timed out", "Timeout", "overloaded", "unreachable")):
        # Overloaded - "high demand" blips pass quickly. Benching for five
        # minutes left a whole chain empty in testing and failed two agents.
        _dead[(provider, model)] = time.monotonic() + 30
        return True
    if kind == "refusal":
        return False                  # the next model in the chain may answer; retrying this one won't
    return False


def chat(agent: dict, system: str, messages: list[dict], tools: list[dict], effort: str | None = None) -> dict:
    """-> {"text", "tool_calls", "provider", "model", "note", "_via", ...}. Blocking.

    Tries the agent's models in order (its own, then the backups for every agent). `effort` is the
    reasoning effort for models that think: none, low or medium."""
    entries = chain(agent)
    if not entries:
        raise NoProvider("This agent has no model set (Settings ▸ Models & agents).")
    first = entries[0]
    reasons: list[str] = []
    last, any_transient = None, True
    for round_, wait in enumerate(BACKOFF):
        if round_ and not any_transient:
            break
        if wait:
            log.info("every model for %s is busy; retrying in %ss", agent.get("name"), wait)
            time.sleep(wait)
        any_transient = False
        for provider, model in entries:
            problem = _problem(provider) if provider != "codex" else "Codex runs through the Codex runner."
            if problem:
                if round_ == 0:
                    reasons.append(f"{provider}/{model}: {problem}")
                continue
            if round_ == 0 and _skip(provider, model):
                any_transient = True          # benched a moment ago: try again in a later round
                continue
            try:
                started = time.monotonic()
                out = _call(provider, model, system, messages, tools, effort)
            except KeyProblem as error:
                reasons.append(f"{provider}/{model}: {error}")
                last = error
                continue                      # this provider cannot run now; the next one may
            except Exception as error:
                last = error
                transient = _mark(provider, model, error)
                any_transient |= transient
                if round_ == 0:
                    reasons.append(f"{provider}/{model}: {str(error)[:120]}")
                log.info("agent model %s/%s failed: %s", provider, model, str(error)[:160])
                continue
            served = out.pop("served_by", "") or model
            note = ""
            if (provider, model) != first and reasons:
                note = f"{first[0]}/{first[1]} did not answer ({reasons[0].split(': ', 1)[1][:120]}); {provider}/{model} took over"
            out.update(provider=provider, model=model, note=note, _via=f"{provider}/{served}",
                       seconds=round(time.monotonic() - started, 1))
            return out
    if last is not None and isinstance(last, KeyProblem) and len(reasons) == 1:
        raise last
    detail = "; ".join(reasons)[:600] or str(last)[:300]
    raise RuntimeError(f"no model answered ({', '.join(f'{p}/{m}' for p, m in entries)}): {detail}")


def _call(provider: str, model: str, system: str, messages: list[dict], tools: list[dict],
          effort: str | None) -> dict:
    from mint.agents import catalog
    kind = catalog.all_providers()[provider]["kind"]
    if kind == "responses":
        return _responses(model, system, messages, tools, effort=effort)
    if kind == "gemini":
        return _gemini(model, system, messages, tools)
    if kind == "anthropic":
        from mint.agents import anthropic_provider
        try:
            return anthropic_provider.chat(model, system, messages, tools, effort)
        except anthropic_provider.AnthropicError as error:
            if error.kind == "key":
                BAD_KEYS["anthropic"] = (catalog.key("anthropic"), str(error))
                raise KeyProblem(str(error)) from error
            if error.kind == "credit":
                NO_CREDIT["anthropic"] = (catalog.key("anthropic"), str(error), time.monotonic())
                raise KeyProblem(str(error)) from error
            raise
    return _openai(model, system, messages, tools, provider=provider, effort=effort)


# --- Gemini ------------------------------------------------------------------------

def _gemini(model: str, system: str, messages: list[dict], tools: list[dict]) -> dict:
    from google import genai
    from google.genai import types

    from mint.core import gemini_keys
    client = gemini_keys.client(http_options=types.HttpOptions(timeout=90000,
                                                               retry_options=types.HttpRetryOptions(attempts=1)))
    contents = []
    for m in messages:
        if m["role"] == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m["content"])]))
        elif m["role"] == "assistant":
            if m.get("_gemini") is not None and m.get("_via", f"gemini/{model}") == f"gemini/{model}":
                # Gemini 3 wants its own reply back verbatim: the function-call
                # parts carry a thought_signature, and without it the next call
                # fails with 400 INVALID_ARGUMENT.
                contents.append(m["_gemini"])
                continue
            parts = [types.Part(text=m["content"])] if m.get("content") else []
            for call in m.get("tool_calls") or []:
                # Calls another model made (before a fallback) have no signature of Gemini's own: Google's
                # documented placeholder lets them through.
                parts.append(types.Part(function_call=types.FunctionCall(
                    id=call["id"], name=call["name"], args=call["args"]),
                    thought_signature=b"skip_thought_signature_validator"))
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
    """OpenAI-compatible Chat Completions: OpenRouter, Groq, xAI, Ollama, custom endpoints."""
    from mint.agents import catalog

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
            if m.get("_reasoning_details") and m.get("_via", f"{provider}/{model}") == f"{provider}/{model}":
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
    style = catalog.EFFORT_STYLE.get(provider, "")
    if effort in EFFORTS and style == "reasoning":
        body["reasoning"] = {"effort": effort}
    elif effort in EFFORTS and style == "reasoning_effort" and "gpt-oss" in model:
        body["reasoning_effort"] = "low" if effort == "none" else effort
    base = catalog.base_url(provider)
    key = catalog.key(provider)
    headers = {"Authorization": f"Bearer {key or 'none'}"}
    if provider == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/savka777/jev-use", "X-Title": "Mint"})
    try:
        response = _client().post(f"{base}/chat/completions", json=body, timeout=180, headers=headers)
    except Exception as error:
        raise RuntimeError(f"unreachable: {type(error).__name__} {str(error)[:160]}") from error
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code in (401, 403):
        message = (data.get("error") or {}).get("message", response.text[:200]) if isinstance(data, dict) else ""
        label = catalog.all_providers().get(provider, {}).get("label", provider)
        problem = (f"{label} refused the API key ({response.status_code}: {str(message)[:160]}). The user must make "
                   "a new key and add it in Settings ▸ Models & agents.")
        BAD_KEYS[provider] = (key, problem)
        raise KeyProblem(problem)
    if _no_credit(response.status_code, data):
        raise KeyProblem(_flag_no_credit(provider, data))      # retrying cannot help
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
    _count(f"{provider}/{body.get('model')}", usage.get("prompt_tokens"), usage.get("completion_tokens"))
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

    mine = f"openai/{model}"
    last = max((i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("_response_id")
                and m.get("_via", mine) == mine), default=None)
    items = []
    for m in (messages[last + 1:] if last is not None else messages):
        if m["role"] == "user":
            items.append({"role": "user", "content": m["content"]})
        elif m["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": str(m["tool_call_id"]), "output": str(m["content"])})
        elif m["role"] == "assistant":
            # Another model's turn (a run that fell back): its text and calls, so each output has its call.
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for call in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": str(call["id"]), "name": call["name"],
                              "arguments": json.dumps(call.get("args") or {})})
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
    if _no_credit(response.status_code, data):
        raise KeyProblem(_flag_no_credit("openai", data))      # retrying cannot help
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
    _count(body.get("model"), usage.get("input_tokens"), usage.get("output_tokens"),
           (usage.get("output_tokens_details") or {}).get("reasoning_tokens"))
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
