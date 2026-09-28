"""Plain Gemini requests for the background jobs (not the Live voice session).

`generate` tries a chain of models and copes with what the free tier does all the
time: 503 "high demand" (retried once after a pause, then skipped for a minute),
429 / daily quota (skipped for two minutes / an hour). It has a long timeout -
transcribing audio takes 20-40 s - unlike ground._generate, which is tuned for
pointing at the screen within seconds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

log = logging.getLogger("mint.core.llm")

LITE = [m for m in (os.environ.get("MINT_LITE_MODEL"), "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                    "gemini-flash-lite-latest", "gemini-3.7-flash", "gemini-3.5-flash") if m]

_client_cache = None
_busy: dict[str, float] = {}      # model -> monotonic time it may be tried again


def client():
    global _client_cache
    if _client_cache is None:
        from google import genai
        from google.genai import types
        from mint.core import config
        _client_cache = genai.Client(api_key=os.environ[config.API_KEY_ENV],
                                     http_options=types.HttpOptions(timeout=180_000))
    return _client_cache


def generate(contents, models: list[str] | None = None, json_mode: bool = False,
             config_extra: dict | None = None) -> tuple[str, str]:
    """First model in `models` that answers -> (text, model)."""
    from google.genai import types
    models = models or LITE
    last = None
    for attempt in range(2):
        for model in models:
            if time.monotonic() < _busy.get(model, 0):
                continue
            try:
                reply = client().models.generate_content(
                    model=model, contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                        **({"response_mime_type": "application/json"} if json_mode else {}), **(config_extra or {})))
                return str(reply.text or ""), model
            except Exception as error:
                last, text = error, str(error)
                log.info("%s: %s", model, text[:120])
                if "PerDay" in text:
                    _busy[model] = time.monotonic() + 3600
                elif any(code in text for code in ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")):
                    _busy[model] = time.monotonic() + 120
                elif attempt and any(code in text for code in ("503", "UNAVAILABLE", "504", "DEADLINE")):
                    _busy[model] = time.monotonic() + 60
        if attempt == 0:
            time.sleep(3)
    raise RuntimeError(f"no Gemini model answered: {str(last)[:160]}")


def parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    return json.loads(text)


def ask_json(prompt: str, models: list[str] | None = None):
    """One JSON answer, or None when no model answered or it was not JSON."""
    try:
        text, _ = generate(prompt, models, json_mode=True)
        return parse_json(text)
    except Exception as error:
        log.info("ask_json: %s", str(error)[:160])
        return None
