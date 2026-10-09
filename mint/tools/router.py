"""Which tools and how-to a request needs, picked by a small model instead of the voice model's own search.

The voice session declares only a few tools and a short instruction (tool_diet.CORE, config.SYSTEM_INSTRUCTION:
~3k tokens re-read on every tool step, where it used to be ~26k). The rest is grouped in tool_diet.FAMILIES - a
group's tools plus its how-to chunk (guides.py, the modules' PROMPT). For each request this module picks the
groups, and the session hands them over in find_tools' answer and in the context of the request's first tool
result, so the voice model rarely has to search by itself.

Who decides (pref "router"; "auto" by default):
    jev      Jev (TypeSafe) ranks the groups in one call - ~0.2 s - when a TypeSafe key is set
    gemini   a cheap Gemini text model (llm.LITE: flash-lite first) answers with the group ids
    words    word overlap only (also the fallback when the model is unreachable or slow)
Whichever answers, the ids are checked against the real list - a model can't invent a group (jev-ultrafast's
validate_choice). Answers are cached per request text.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

log = logging.getLogger("mint.tools.router")

MAX_GROUPS = 3
TIMEOUT = 4.0              # seconds the model may take before the word match decides
JEV_FLOOR = 0.12           # a group Jev gives less than this is left out
_pool = ThreadPoolExecutor(2, thread_name_prefix="mint-router")
_ahead = ThreadPoolExecutor(1, thread_name_prefix="mint-router-ahead")     # prefetch: never waits on _pool's slots
_lock = threading.Lock()
_cache: OrderedDict[str, Route] = OrderedDict()
CACHE = 64


@dataclass
class Route:
    groups: list[str] = field(default_factory=list)      # tool_diet.FAMILIES keys, most needed first
    source: str = "words"                                  # jev | gemini | words
    took: float = 0.0


def mode() -> str:
    try:
        from mint.core import prefs
        wanted = str(prefs.get("router") or "auto").lower()
    except Exception:
        wanted = "auto"
    if wanted in ("jev", "gemini", "words"):
        return wanted
    from mint.core import jev
    return "jev" if jev.available() else "gemini"


def _families() -> dict:
    from mint.tools import diet as tool_diet
    return tool_diet.FAMILIES


def options() -> dict[str, str]:
    """g0, g1... -> a one-line description of the group: what it is for and its tools."""
    out = {}
    for i, (family, info) in enumerate(_families().items()):
        hint = f" - {info['hint']}" if info.get("hint") else ""
        out[f"g{i}"] = f"{family}{hint} (tools: {', '.join(info['tools'])})"
    return out


def _ids_to_groups(ids) -> list[str]:
    names = list(_families())
    groups = []
    for gid in ids or []:
        text = str(gid).strip().lower()
        if text.startswith("g") and text[1:].isdigit() and int(text[1:]) < len(names):
            family = names[int(text[1:])]
            if family not in groups:
                groups.append(family)
    return groups[:MAX_GROUPS]


# --- the deciders -----------------------------------------------------------------------------------------------

_INSTRUCTIONS = ("A voice assistant on a Mac has a few tools of its own (open apps and sites, click, type, scroll, "
                 "read the window, look at the screen) and these groups of further tools and how-tos. Which group "
                 "does `request` need? Pick the group whose tools would do the job; choose none for chat, a "
                 "question it can answer from knowledge, or a job its own tools already do.")


def _by_jev(request: str) -> list[str] | None:
    from mint.core import jev
    ranked, _ = jev.rank(request, options(), _INSTRUCTIONS, timeout=TIMEOUT)
    if ranked is None:
        return None
    return _ids_to_groups([gid for gid, p in ranked if p >= JEV_FLOOR])


def _by_gemini(request: str) -> list[str] | None:
    from mint.core import llm
    listing = "\n".join(f"{gid}: {text}" for gid, text in options().items())
    prompt = (f"{_INSTRUCTIONS}\n\nGroups:\n{listing}\n\nRequest: {request}\n\n"
              f"Answer JSON only: {{\"ids\": [up to {MAX_GROUPS} group ids, most needed first]}} - [] for none.")
    for model in llm.LITE[:2]:
        if time.monotonic() < llm._busy.get(model, 0):
            continue
        try:
            from google.genai import types
            # Minimal thinking: ~0.9 s on flash-lite (9 Oct); its default thinking took 2-18 s.
            reply = llm.client().models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0, response_mime_type="application/json", max_output_tokens=60,
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
            answer = llm.parse_json(str(reply.text or "{}"))
            return _ids_to_groups(answer.get("ids") if isinstance(answer, dict) else answer)
        except Exception as error:
            log.info("router %s: %s", model, str(error)[:120])
            if any(code in str(error) for code in ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")):
                llm._busy[model] = time.monotonic() + 120
    return None


_CHAT = set("how what who why when where which are was were hey hi hello mint thank thanks okay ok yes no good "
            "great nice cool sorry please tell".split())     # words of chat, not of a job


def by_words(request: str, limit: int = MAX_GROUPS) -> list[str]:
    """Groups whose words, label, tools and how-to share words with the request (idf-weighted)."""
    from mint.tools import diet as tool_diet
    wanted = set(tool_diet._words(request)) - _CHAT
    if not wanted:
        return []
    docs = {}
    for family, info in _families().items():
        docs[family] = (set(tool_diet._words(f"{family} {info.get('label', '')} {info['words']} "
                                            f"{' '.join(info['tools'])}")),
                        set(tool_diet._words(tool_diet.guide(family))))
    total = len(docs)
    scores = {}
    for family, (strong, weak) in docs.items():
        score = 0.0
        for word in wanted:
            have = sum(1 for s, w in docs.values() if word in s or word in w)
            if not have:
                continue
            idf = math.log(1 + total / have)
            score += idf * (1.0 if word in strong else 0.25 if word in weak else 0.0)
        if score > 0:
            scores[family] = score
    best = sorted(scores, key=lambda f: -scores[f])
    if not best:
        return []
    floor = max(scores[best[0]] * 0.5, 1.2)              # a lone common word ("open", "my") is no match
    return [f for f in best if scores[f] >= floor][:limit]


# --- route -----------------------------------------------------------------------------------------------------

def _key(request: str) -> str:
    return " ".join(str(request or "").lower().split())[:300]


def route(request: str, timeout: float = TIMEOUT) -> Route:
    """The groups `request` needs (cached). The model gets `timeout` seconds; then the word match decides."""
    key = _key(request)
    if not key:
        return Route()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    started = time.monotonic()
    how = mode()
    groups, source, late = None, "words", None
    if how in ("jev", "gemini"):
        job = _pool.submit(_by_jev if how == "jev" else _by_gemini, request)
        try:
            groups = job.result(timeout=timeout)
            source = how if groups is not None else "words"
        except FutureTimeout:
            log.info("router: %s took over %.1f s", how, timeout)
            late = job                      # its answer is cached when it comes, for the next look-up
        except Exception:
            log.exception("router %s failed", how)
    if groups is None:
        groups = by_words(request)
    found = Route(groups, source, time.monotonic() - started)
    print(f"  [router: {found.source}, {found.took:.2f} s -> {[_families()[g].get('label', g) for g in groups]}]",
          flush=True)
    if late is not None:
        late.add_done_callback(lambda job: _remember(key, job, how, started))
    else:
        _store(key, found)
    return found


def _store(key: str, found: Route) -> None:
    with _lock:
        _cache[key] = found
        while len(_cache) > CACHE:
            _cache.popitem(last=False)


def _remember(key: str, job, how: str, started: float) -> None:
    try:
        groups = job.result()
    except Exception:
        return
    if groups is not None:
        _store(key, Route(groups, how, time.monotonic() - started))


def prefetch(request: str) -> None:
    """Start deciding in the background (the answer is cached for route)."""
    if _key(request) and _key(request) not in _cache:
        _ahead.submit(route, request)


def reset() -> None:
    with _lock:
        _cache.clear()
