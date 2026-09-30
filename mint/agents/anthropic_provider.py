"""Claude models for the sub-agents, through the official Anthropic SDK (ANTHROPIC_API_KEY).

Agents keep their conversation in the neutral shape of providers.py. A Claude turn is stored twice: the
neutral text + tool calls, and the raw content blocks (`_anthropic`, with the model that wrote them).
Claude's thinking blocks must go back unchanged when the same model continues, so the raw blocks are
replayed for that model; any other model (after a fallback) gets the neutral form.

Thinking: current Claude models think adaptively and are steered with `output_config.effort`; Mint's
none/low/medium map to low/low/medium (Claude Opus 5.5 cannot switch thinking off). Haiku 4.5 takes no
effort setting and runs without thinking here.

Refusals: on claude-opus-5-5, claude-fable-5-1 and claude-sonnet-5-5 the request opts into Anthropic's
server-side fallback (`fallbacks: "default"`), which re-runs a request the safety classifiers declined on
the model Anthropic recommends for that category. A refusal that still comes back is an error here, so
the agent's own backup chain takes over.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("mint.agents")

MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_EFFORT = {"none": "low", "low": "low", "medium": "medium"}
_clients: dict[tuple, object] = {}


class AnthropicError(RuntimeError):
    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind          # "key", "credit", "quota", "busy", "missing", "refusal", ""


def _client():
    import anthropic

    from mint.agents import catalog
    key, base = catalog.key("anthropic"), catalog.base_url("anthropic")
    cached = _clients.get((key, base))
    if cached is None:
        # Retries are the agent runtime's job (it moves on to backup models); the SDK's own two
        # retries would only hold up a fallback.
        cached = anthropic.Anthropic(api_key=key, base_url=base or None, max_retries=0, timeout=240.0)
        _clients[(key, base)] = cached
    return cached


def list_models() -> list[str]:
    return [m.id for m in _client().models.list(limit=100)]


def _tool_id(raw: str) -> str:
    """Tool-use ids must match ^[a-zA-Z0-9_-]+$ - ids from other providers may not."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or "call"))[:64] or "call"


def _supports_effort(model: str) -> bool:
    return not model.startswith("claude-haiku") and not re.search(r"claude-(3|sonnet-4-5|opus-4-5|opus-4-1)", model)


def _messages(model: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        if m["role"] == "user":
            add("user", [{"type": "text", "text": m.get("content") or "(empty)"}])
        elif m["role"] == "assistant":
            raw = m.get("_anthropic")
            if raw and m.get("_via") == f"anthropic/{model}":
                add("assistant", raw)            # thinking blocks go back exactly as they came
                continue
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for call in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": _tool_id(call["id"]), "name": call["name"],
                               "input": call.get("args") or {}})
            if blocks:
                add("assistant", blocks)
        elif m["role"] == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": _tool_id(m.get("tool_call_id")),
                          "content": str(m.get("content") or "(no output)")}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return out


def chat(model: str, system: str, messages: list[dict], tools: list[dict], effort: str | None = None) -> dict:
    import anthropic

    body = {"model": model, "max_tokens": MAX_TOKENS, "system": system, "messages": _messages(model, messages)}
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                         for t in tools]
    if effort in _EFFORT and _supports_effort(model):
        body["output_config"] = {"effort": _EFFORT[effort]}
    try:
        if model in _FALLBACK_MODELS and not catalog_base_is_custom():
            response = _client().beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **body)
        else:
            response = _client().messages.create(**body)
    except anthropic.AuthenticationError as error:
        raise AnthropicError(f"Anthropic refused the API key ({error.message[:160]}).", "key") from error
    except anthropic.PermissionDeniedError as error:
        raise AnthropicError(f"Anthropic refused the API key ({error.message[:160]}).", "key") from error
    except anthropic.NotFoundError as error:
        raise AnthropicError(f"404 Anthropic has no model '{model}' for this key ({error.message[:120]}).",
                             "missing") from error
    except anthropic.RateLimitError as error:
        raise AnthropicError(f"429 Anthropic rate limit ({error.message[:160]}).", "quota") from error
    except anthropic.BadRequestError as error:
        text = error.message or ""
        if "credit balance" in text.lower() or "billing" in text.lower():
            raise AnthropicError(f"The Anthropic account has no credits left ({text[:160]}). Add credits at "
                                 "console.anthropic.com/settings/billing.", "credit") from error
        raise AnthropicError(f"400 {text[:300]}") from error
    except anthropic.APIStatusError as error:
        kind = "busy" if error.status_code >= 500 else ""
        raise AnthropicError(f"{error.status_code} {(error.message or '')[:200]}", kind) from error
    except anthropic.APIConnectionError as error:
        raise AnthropicError(f"timed out / unreachable: {str(error)[:160]}", "busy") from error

    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise AnthropicError(f"Claude declined this request (refusal{f': {category}' if category else ''}).",
                             "refusal")
    text, calls = [], []
    for block in response.content:
        if block.type == "text":
            text.append(block.text)
        elif block.type == "tool_use":
            calls.append({"id": block.id, "name": block.name, "args": dict(block.input or {})})
    raw = [block.model_dump(exclude_none=True) for block in response.content]
    usage = response.usage
    try:
        from mint.agents.providers import _count
        _count(f"anthropic/{response.model}", usage.input_tokens, usage.output_tokens)
    except Exception:
        pass
    return {"text": "".join(text).strip(), "tool_calls": calls, "_anthropic": raw,
            "served_by": response.model}


def catalog_base_is_custom() -> bool:
    """Server-side fallback exists on the Claude API itself, not behind a custom base URL (a proxy)."""
    from mint.agents import catalog
    return catalog.base_url("anthropic").rstrip("/") not in ("", "https://api.anthropic.com")
