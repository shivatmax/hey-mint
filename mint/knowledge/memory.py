"""The memory bank: what Mint knows about the user, one fact per block.

    memory/bank.json     the blocks (source of truth, mode 600)
    memory/MEMORY.md     the same, grouped, for people to read

A block is one fact - "The user's manager is Priya", "Standup is 9 pm IST" -
filed in a group (people, work, accounts, schedule…). Some are pinned ("fixed"
memories: who the user is, and the vocabulary of words speech recognition gets
wrong); pinned blocks are given to every session. Everything else stays out of
the prompt until it is needed, which is what keeps the context small:

* recall(question): Jev reads every block against the question in ONE request -
  an independent yes/no per block (30 blocks in about a second in testing) -
  and only the blocks it marks relevant are returned.
* add(fact): Jev files it in a group and checks whether it replaces an older
  block ("my manager is now Rahul" replaces "my manager is Priya"), so the
  bank is updated in place instead of piling up contradictions.
* extract(conversation): a Flash model pulls durable facts out of what was said,
  in the background, and adds them the same way.

Passwords, keys and card numbers are refused everywhere.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from mint.core import config
from mint.core import jev
from mint.knowledge.skills import has_secret

log = logging.getLogger("mint.knowledge.memory")

ROOT = config.PROJECT_ROOT / "memory"
BANK = ROOT / "bank.json"
VIEW = ROOT / "MEMORY.md"
LEGACY = config.PROJECT_ROOT / "memories.md"

GROUPS = {
    "core": "Who the user is: name, job, company, how they like Mint to behave. Always given.",
    "people": "People in the user's life and work: names, roles, relationships, contact details.",
    "work": "The user's work: team, projects, tools, processes, channels, repos.",
    "accounts": "Which account, email, Chrome profile or workspace is which (never passwords).",
    "schedule": "Routines, recurring meetings, dates, birthdays, deadlines.",
    "preferences": "Likes, dislikes and how the user wants things done.",
    "places": "Locations, addresses, where files and things are kept.",
    "vocabulary": 'Words speech recognition mishears, as: "heard" means meant. Always given.',
    "misc": "Anything else worth keeping.",
}
PINNED_GROUPS = {"core", "vocabulary"}
RELEVANT = 0.45         # Jev's yes-probability for a block to count as relevant
BATCH = 60              # blocks judged per Jev request
_lock = threading.RLock()


# --- storage ------------------------------------------------------------------------------

def _load() -> list[dict]:
    try:
        blocks = json.loads(BANK.read_text())
        return blocks if isinstance(blocks, list) else []
    except FileNotFoundError:
        return _migrate()
    except (OSError, json.JSONDecodeError) as error:
        log.warning("memory bank unreadable: %s", error)
        return []


def _save(blocks: list[dict]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = BANK.with_suffix(".tmp")
    tmp.write_text(json.dumps(blocks, indent=1, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(BANK)
    _write_view(blocks)


def _write_view(blocks: list[dict]) -> None:
    lines = ["# What Mint remembers", "",
             "One fact per line. Pinned (📌) facts are given to every conversation; the rest are",
             "looked up when a question needs them. Ask Mint to change them, or edit",
             "memory/bank.json.", ""]
    for group in sorted({b["group"] for b in blocks}, key=lambda g: (g not in PINNED_GROUPS, g)):
        lines.append(f"## {group}")
        for b in blocks:
            if b["group"] == group:
                lines.append(f"- {'📌 ' if b.get('pinned') else ''}{b['text']}  <!-- {b['id']} -->")
        lines.append("")
    VIEW.write_text("\n".join(lines))
    os.chmod(VIEW, 0o600)


def _migrate() -> list[dict]:
    """Bring over memories.md (the first, flat version) once."""
    blocks: list[dict] = []
    try:
        for line in LEGACY.read_text().splitlines():
            match = re.match(r"^- (?:\[([^\]]+)\] )?(.*?)(?: \((\d{4}-\d\d-\d\d)\))?$", line)
            if match and match.group(2):
                group = _group_name(match.group(1) or "misc")
                blocks.append(_block(len(blocks) + 1, match.group(2), group, group in PINNED_GROUPS, "user"))
    except OSError:
        return []
    if blocks:
        _save(blocks)
    return blocks


def _block(n: int, text: str, group: str, pinned: bool, source: str) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M")
    return {"id": f"m{n}", "group": group, "text": text, "pinned": pinned,
            "created": now, "updated": now, "source": source, "hits": 0}


def _next_id(blocks: list[dict]) -> int:
    return max([int(b["id"][1:]) for b in blocks if re.match(r"^m\d+$", b["id"])] + [0]) + 1


def _group_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:30] or "misc"
    synonyms = {"person": "people", "contacts": "people", "account": "accounts", "profiles": "accounts",
                "prefs": "preferences", "preference": "preferences", "calendar": "schedule",
                "dates": "schedule", "job": "work", "me": "core", "about-me": "core", "identity": "core",
                "words": "vocabulary", "corrections": "vocabulary", "place": "places", "location": "places"}
    return synonyms.get(name, name)


def blocks() -> list[dict]:
    with _lock:
        return _load()


def entries(group: str) -> list[str]:
    """Plain texts of one group, e.g. entries("vocabulary")."""
    group = _group_name(group)
    return [b["text"] for b in blocks() if b["group"] == group]


# --- reading ---------------------------------------------------------------------------------

def _describe(b: dict) -> str:
    return f"({b['group']}) {b['text']}"


def relevant(query: str, limit: int = 6, include_pinned: bool = False) -> list[dict]:
    """The blocks that matter for `query`, judged by Jev block by block."""
    pool = [b for b in blocks() if include_pinned or not b.get("pinned")]
    if not pool or not query.strip():
        return []
    started = time.monotonic()
    if not jev.available():
        chosen = _gemini_relevant(query, pool[-400:], limit)
        if chosen is None:
            chosen = _lexical(query, pool, limit)
        log.info("recall %r -> %d blocks in %.1fs (Gemini)", query[:60], len(chosen), time.monotonic() - started)
        if chosen:
            _touch([b["id"] for b in chosen])
            _note_use(chosen, query)
        return chosen
    if len(pool) > BATCH:
        # Narrow a big bank first: one choice question ranks every block, and
        # the best BATCH go on to be judged one by one.
        options = {b["id"]: _describe(b)[:200] for b in pool[-250:]}
        ranked, _ = jev.rank(query, options, "Which remembered fact is most relevant to `request`?",
                             timeout=5.0)
        if ranked:
            order = [i for i, _ in ranked]
            pool = sorted(pool, key=lambda b: order.index(b["id"]) if b["id"] in order else 10**6)[:BATCH]
        else:
            pool = pool[-BATCH:]
    questions = {
        b["id"]: {"type": "noul",
                  "instructions": f"Remembered fact: '{b['text']}'. Is this fact "
                                  "needed or clearly helpful to answer or act on `request`?",
                  "criteria": {"true": "Needed or clearly helpful for this request.",
                               "false": "Not relevant to this request."}}
        for b in pool}
    answers = jev.ask({"request": query}, questions, timeout=6.0)
    if answers is None:
        chosen = _gemini_relevant(query, pool, limit)
        if chosen is None:
            chosen = _lexical(query, pool, limit)
        log.info("recall: Jev unreachable, fallback gave %d", len(chosen))
    else:
        scored = sorted(((jev.yes(answers, b["id"]), b) for b in pool), key=lambda x: -x[0])
        chosen = [b for p, b in scored if p >= RELEVANT][:limit]
    log.info("recall %r -> %d blocks in %.1fs", query[:60], len(chosen), time.monotonic() - started)
    if chosen:
        _touch([b["id"] for b in chosen])
        _note_use(chosen, query)
    return chosen


def _lexical(query: str, pool: list[dict], limit: int) -> list[dict]:
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 3}
    scored = []
    for b in pool:
        have = set(re.findall(r"[a-z0-9]+", b["text"].lower()))
        overlap = len(words & have)
        if overlap:
            scored.append((overlap, b))
    return [b for _, b in sorted(scored, key=lambda x: -x[0])[:limit]]


def _numbered(pool: list[dict]) -> str:
    return "\n".join(f"{b['id']}: ({b['group']}) {b['text']}" for b in pool)


def _gemini_relevant(query: str, pool: list[dict], limit: int) -> list[dict] | None:
    """Without Jev (no TypeSafe key): one Flash Lite request picks the relevant facts.
    None when Gemini did not answer (the caller falls back to word matching)."""
    from mint.core import llm
    answer = llm.ask_json(
        "A voice assistant keeps these facts about its user, one per line (id: (group) fact):\n"
        f"{_numbered(pool)}\n\nWhich facts are needed or clearly helpful to answer or act on this request: "
        f"{query!r}? Think of synonyms (boss = manager, mum = mother). Return JSON: {{\"ids\": [...]}}, most "
        f"useful first, at most {limit}; [] if none.")
    if not isinstance(answer, dict):
        return None
    by_id = {b["id"]: b for b in pool}
    return [by_id[i] for i in answer.get("ids") or [] if i in by_id][:limit]


def _gemini_file(text: str, group: str, bank: list[dict]) -> tuple[str, dict | None]:
    """Without Jev: which group a new fact goes in, and which saved fact it replaces."""
    from mint.core import llm
    groups = dict(GROUPS)
    for b in bank:
        groups.setdefault(b["group"], f"The user's '{b['group']}' memories.")
    pool = bank[-200:]
    answer = llm.ask_json(
        f"A voice assistant is saving a new fact about its user: {text!r}.\n"
        + ("" if group else "Groups:\n" + "\n".join(f"- {k}: {v}" for k, v in groups.items()) + "\n")
        + f"Saved facts (id: (group) fact):\n{_numbered(pool) or '(none)'}\n\n"
        "Does the new fact REPLACE one saved fact - the same subject with a new or corrected value ('my "
        "manager is Rahul' replaces 'my manager is Priya'; 'I also like tea' does not replace 'I like "
        "coffee')? Return JSON: {\"group\": \"<group name>\", \"replaces\": \"<id or null>\"}.")
    if not isinstance(answer, dict):
        return group or "misc", None
    chosen = group or _group_name(str(answer.get("group") or "misc"))
    old = next((b for b in pool if b["id"] == answer.get("replaces")), None)
    return chosen, old


def _keep_history(block: dict, before: str) -> None:
    """A fact that changed keeps what it used to say, and until when ('what was my
    manager before?'), instead of losing it."""
    if before and before != block["text"]:
        history = block.setdefault("history", [])
        history.append({"text": before, "until": time.strftime("%Y-%m-%d")})
        block["history"] = history[-5:]


_used: list[tuple[float, str, str, str]] = []     # (when, id, fact, the request it was fetched for)


def _note_use(chosen: list[dict], query: str) -> None:
    """Remember which facts were handed to the conversation, for "which memory did you use?"."""
    now = time.time()
    for b in chosen:
        _used.append((now, b["id"], b["text"], query[:120]))
    del _used[:-60]


def used(minutes: float = 30) -> str:
    """The facts Mint was given in the last `minutes`, newest first, plus the fixed ones."""
    since = time.time() - minutes * 60
    seen, lines = set(), []
    for when, bid, text, query in reversed(_used):
        if when < since or bid in seen:
            continue
        seen.add(bid)
        lines.append(f"- {text}  (looked up {time.strftime('%H:%M', time.localtime(when))} for: {query!r})")
    fixed = [b["text"] for b in blocks() if b.get("pinned")]
    out = ("Facts looked up recently:\n" + "\n".join(lines[:15])) if lines else \
        "No remembered facts were looked up in the last half hour."
    if fixed:
        out += "\nAlways known (fixed memories): " + "; ".join(fixed[:12])
    return out + ("\nTell the user which of these shaped the answer. If one is wrong or outdated, offer to fix it "
                  "(update_memory) or forget it.")


def _touch(ids: list[str]) -> None:
    with _lock:
        bank = _load()
        for b in bank:
            if b["id"] in ids:
                b["hits"] = int(b.get("hits", 0)) + 1
        _save(bank)


def recall(query: str = "") -> str:
    if not blocks():
        return "Nothing is remembered yet."
    if not query.strip():
        return listing()
    found = relevant(query)
    if not found:
        return f"Nothing remembered is relevant to '{query}'."
    return "\n".join(f"- ({b['group']}) {b['text']}" + _was(b) for b in found)


def _was(b: dict) -> str:
    history = b.get("history") or []
    return (" [before: " + "; ".join(f"'{h['text']}' until {h['until']}" for h in history[-2:]) + "]") \
        if history else ""


def pinned_text(limit: int = 1800) -> str:
    lines = [f"- ({b['group']}) {b['text']}" for b in blocks() if b.get("pinned")]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[-limit:]


def index_text() -> str:
    counts: dict[str, int] = {}
    for b in blocks():
        if not b.get("pinned"):
            counts[b["group"]] = counts.get(b["group"], 0) + 1
    return ", ".join(f"{g} ({n})" for g, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def listing(group: str = "") -> str:
    bank = [b for b in blocks() if not group or b["group"] == _group_name(group)]
    if not bank:
        return "Nothing remembered" + (f" under {group}." if group else " yet.")
    out, last = [], None
    for b in sorted(bank, key=lambda b: (b["group"] not in PINNED_GROUPS, b["group"])):
        if b["group"] != last:
            out.append(f"{b['group']}:")
            last = b["group"]
        out.append(f"  - {'[fixed] ' if b.get('pinned') else ''}{b['text']}")
    return "\n".join(out)


# --- writing ---------------------------------------------------------------------------------

def _choose_group(text: str) -> str:
    groups = dict(GROUPS)
    for b in blocks():
        groups.setdefault(b["group"], f"The user's '{b['group']}' memories.")
    options = {name: f"{name}: {desc}" for name, desc in groups.items()}
    ranked, _ = jev.rank(text, options, "Which memory group should this new fact about the user be filed in?",
                         timeout=5.0)
    return ranked[0][0] if ranked and ranked[0][1] >= 0.3 else "misc"


def _file(text: str, group: str, bank: list[dict]) -> tuple[str, dict | None]:
    """One Jev request: which group the fact belongs in (unless given), and
    whether it replaces an existing block. Two separate calls took 2-4 s."""
    pool = bank[-BATCH:]
    questions = {
        b["id"]: {"type": "noul",
                  "instructions": f"Existing memory: '{b['text']}'. Is `request` a newer or corrected "
                                  "version of this same fact, so it should replace it (same subject, "
                                  "new value), rather than a separate fact?",
                  "criteria": {"true": "Same fact, updated or corrected: replace the old one.",
                               "false": "A different fact: keep both."}}
        for b in pool}
    if not group:
        groups = dict(GROUPS)
        for b in bank:
            groups.setdefault(b["group"], f"The user's '{b['group']}' memories.")
        criteria = {name: f"{name}: {desc}" for name, desc in groups.items()}
        questions["group"] = {"type": "choice", "criteria": criteria,
                              "instructions": "Which memory group should `request`, a new fact about "
                                              "the user, be filed in?"}
    if not questions:
        return group or "misc", None
    if not jev.available():
        return _gemini_file(text, group, bank)
    answers = jev.ask({"request": text}, questions, timeout=6.0, retries=1)
    if answers is None:
        return _gemini_file(text, group, bank)
    if not group:
        group = (answers.get("group") or {}).get("choice") or "misc"
    old = max(pool, key=lambda b: jev.yes(answers, b["id"])) if pool else None
    if old is not None and jev.yes(answers, old["id"]) < 0.6:
        old = None
    return group, old


def _replaces(text: str, group: str, bank: list[dict]) -> dict | None:
    """An existing block this new fact updates or contradicts, judged by Jev."""
    pool = [b for b in bank if b["group"] == group] or bank
    pool = pool[-BATCH:]
    if not pool:
        return None
    questions = {
        b["id"]: {"type": "noul",
                  "instructions": f"Existing memory: '{b['text']}'. Is `request` a newer or corrected "
                                  "version of this same fact, so it should replace it (same subject, "
                                  "new value), rather than a separate fact?",
                  "criteria": {"true": "Same fact, updated or corrected: replace the old one.",
                               "false": "A different fact: keep both."}}
        for b in pool}
    answers = jev.ask({"request": text}, questions, timeout=6.0, retries=1)
    if not answers:
        return None
    best = max(pool, key=lambda b: jev.yes(answers, b["id"]))
    return best if jev.yes(answers, best["id"]) >= 0.6 else None


_RELATIVE = re.compile(r"\b(today|tonight|tomorrow|yesterday|this (morning|evening|week|weekend|month)|"
                       r"next (week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                       r"(on|this) (monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I)


def add(text: str, group: str = "", pinned: bool | None = None, source: str = "user") -> str:
    text = " ".join(str(text).split())
    if not text:
        return "Nothing to remember."
    if has_secret(text):
        return ("Refused: that looks like a password, key or card number. Those are never stored; "
                "use a password manager.")
    if _RELATIVE.search(text) and "(said " not in text:
        # "tomorrow" means nothing next week: keep the day it was said, so it can be read (and tidied) later.
        text += f" (said {time.strftime('%a %d %b %Y')})"
    with _lock:
        snapshot = _load()
    if any(b["text"].lower() == text.lower() for b in snapshot):
        return "Already remembered."
    # Filing asks Jev or Gemini (seconds, up to minutes when Gemini is overloaded): not under the
    # lock, which recall and every other memory change need.
    group, old_snapshot = _file(text, _group_name(group) if group else "", snapshot)
    with _lock:
        bank = _load()
        if any(b["text"].lower() == text.lower() for b in bank):
            return "Already remembered."
        old = next((b for b in bank if old_snapshot is not None and b["id"] == old_snapshot["id"]), None)
        if pinned is None:
            pinned = group in PINNED_GROUPS
        if old is not None:
            before = old["text"]
            old.update(text=text, updated=time.strftime("%Y-%m-%d %H:%M"), source=source,
                       pinned=bool(pinned or old.get("pinned")))
            _keep_history(old, before)
            _save(bank)
            return f"Updated memory ({old['group']}): '{before}' is now '{text}'."
        bank.append(_block(_next_id(bank), text, group, bool(pinned), source))
        _save(bank)
    return f"Remembered ({group}{', fixed' if pinned else ''}): {text}"


def _find(what: str, bank: list[dict]) -> tuple[dict | None, str]:
    wanted = what.lower().strip()
    hits = [b for b in bank if wanted and wanted in b["text"].lower()]
    if len(hits) == 1:
        return hits[0], ""
    options = {b["id"]: _describe(b)[:200] for b in bank[-250:]}
    ranked, _ = jev.rank(what, options, "Which remembered fact is `request` talking about?", timeout=6.0) \
        if jev.available() else ([], None)
    if ranked and ranked[0][1] >= 0.5:
        return next(b for b in bank if b["id"] == ranked[0][0]), ""
    if not ranked:
        from mint.core import llm
        answer = llm.ask_json(f"Saved facts (id: (group) fact):\n{_numbered(bank[-250:])}\n\nWhich ONE fact "
                              f"is this talking about: {what!r}? JSON: {{\"id\": \"<id or null>\", \"sure\": "
                              "true only if it clearly means that fact and no other}.")
        hit = next((b for b in bank if isinstance(answer, dict) and answer.get("sure") is True
                    and b["id"] == answer.get("id")), None)
        if hit is not None:
            return hit, ""
    listing_ = "; ".join(b["text"] for b in bank[-12:])
    return None, f"Not sure which memory '{what}' means. Some of what is remembered: {listing_}"


def update(what: str, new_text: str = "", fixed: bool | None = None, group: str = "") -> str:
    if new_text and has_secret(new_text):
        return "Refused: that looks like a password, key or card number."
    with _lock:
        snapshot = _load()
    if not snapshot:
        return "Nothing is remembered yet."
    found, why = _find(what, snapshot)          # may ask Jev or Gemini: not under the lock
    if found is None:
        return why
    with _lock:
        bank = _load()
        block = next((b for b in bank if b["id"] == found["id"]), None)
        if block is None:
            return "That memory is gone."
        before = block["text"]
        if new_text:
            block["text"] = " ".join(new_text.split())
            _keep_history(block, before)
        if fixed is not None:
            block["pinned"] = bool(fixed)
        if group:
            block["group"] = _group_name(group)
        block["updated"] = time.strftime("%Y-%m-%d %H:%M")
        _save(bank)
    change = f"'{before}' -> '{block['text']}'" if new_text else f"'{block['text']}'"
    flag = "" if fixed is None else (" (now fixed)" if fixed else " (no longer fixed)")
    return f"Updated memory: {change}{flag}."


def forget(what: str) -> str:
    with _lock:
        snapshot = _load()
    if not snapshot:
        return "There is nothing remembered to forget."
    found, why = _find(what, snapshot)          # may ask Jev or Gemini: not under the lock
    if found is None:
        return why
    with _lock:
        bank = _load()
        block = next((b for b in bank if b["id"] == found["id"]), None)
        if block is None:
            return "That memory was already gone."
        bank.remove(block)
        _save(bank)
    return f"Forgot: {block['text']}"


def clear() -> None:
    with _lock:
        for path in (BANK, VIEW, ROOT / "changes.jsonl", ROOT / "tidied.json"):
            try:
                path.unlink()
            except OSError:
                pass


# --- learning from conversation -----------------------------------------------------------------

def extract(conversation: str) -> list[str]:
    """Pull durable facts about the user out of a conversation (Flash), and add them."""
    from google import genai
    from google.genai import types

    from mint.knowledge.conversation import MODELS

    known = "\n".join(f"- ({b['group']}) {b['text']}" for b in blocks()[-80:]) or "(nothing yet)"
    prompt = f"""From this conversation between a user and their Mac voice assistant, list durable
facts about the user worth remembering for future conversations: people and roles, accounts and
which is which, work and projects, schedule, preferences, places, how they want things done,
and words the assistant misheard ("heard" means meant). One fact per item, a short standalone
sentence. Only facts the USER stated or clearly confirmed - not guesses, not tasks done today,
not small talk, never passwords, keys or card numbers. Skip anything already known.

Already known:
{known}

Conversation:
{conversation[-8000:]}

JSON only: {{"facts": [{{"text": "...", "group": "{'|'.join(GROUPS)}"}}]}}"""
    client = genai.Client(api_key=os.environ[config.API_KEY_ENV])
    for model in MODELS:
        try:
            reply = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"))
            text = reply.text or ""
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            break
        except Exception as error:
            log.info("fact extraction with %s failed: %s", model, str(error)[:120])
    else:
        return []
    added = []
    for fact in (data.get("facts") or [])[:12]:
        if not isinstance(fact, dict) or not str(fact.get("text", "")).strip():
            continue
        result = add(str(fact["text"]), str(fact.get("group") or ""), source="auto")
        if result.startswith(("Remembered", "Updated")):
            added.append(result)
    if added:
        print(f"  [memory: {len(added)} facts learned from the conversation]", flush=True)
    return added


# --- direct edits (the Skills & Memory window) ----------------------------------------------
# By block id, with no Jev in the loop: an edit made by hand is exact.

def edit_block(block_id: str, text: str | None = None, group: str | None = None,
               pinned: bool | None = None) -> str:
    if text is not None:
        text = " ".join(str(text).split())
        if not text:
            return "A memory cannot be empty; delete it instead."
        if has_secret(text):
            return "Refused: that looks like a password, key or card number."
    with _lock:
        bank = _load()
        block = next((b for b in bank if b["id"] == block_id), None)
        if block is None:
            return f"No memory {block_id}."
        if text is not None:
            block["text"] = text
        if group is not None:
            block["group"] = _group_name(group)
        if pinned is not None:
            block["pinned"] = bool(pinned)
        block["updated"] = time.strftime("%Y-%m-%d %H:%M")
        block["source"] = "edited"
        _save(bank)
    return "Saved."


def delete_block(block_id: str) -> str:
    with _lock:
        bank = _load()
        kept = [b for b in bank if b["id"] != block_id]
        if len(kept) == len(bank):
            return f"No memory {block_id}."
        _save(kept)
    return "Deleted."


def add_exact(text: str, group: str = "misc", pinned: bool = False) -> str:
    """Add a fact exactly as typed, filed where it is told (no Jev)."""
    text = " ".join(str(text).split())
    if not text:
        return "Nothing to add."
    if has_secret(text):
        return "Refused: that looks like a password, key or card number."
    with _lock:
        bank = _load()
        bank.append(_block(_next_id(bank), text, _group_name(group or "misc"), bool(pinned), "edited"))
        _save(bank)
    return "Added."


# --- tidying up, once a day ----------------------------------------------------------------------

TIDIED = ROOT / "tidied.json"
CHANGES = ROOT / "changes.jsonl"
TIDY_EVERY = 20 * 3600

_TIDY = """You look after the memory of a voice assistant: facts about its user, one per line as
"id | group | saved on | fact". Today is {today}. Tidy it, conservatively:

- merge: facts that say the same thing (or one contains the other) -> one fact, keeping every detail.
- expired: facts only true until a date that has passed ("meeting with Sam tomorrow" saved a week ago,
  "flying to Goa on 3 Sep" when it is later). NOT lasting facts, preferences, people or habits.
- regroup: facts in group "misc" that clearly belong in one of: {groups}.

Change nothing else. When unsure, leave it. Return JSON:
{{"merge": [{{"ids": ["m3", "m9"], "text": "merged fact"}}], "expired": [{{"id": "m5", "why": "..."}}],
  "regroup": [{{"id": "m7", "group": "people"}}]}}

FACTS:
{facts}"""


def _log_change(kind: str, detail: dict) -> None:
    with CHANGES.open("a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"), "kind": kind, **detail}, ensure_ascii=False) + "\n")
    os.chmod(CHANGES, 0o600)


def tidy(force: bool = False) -> str:
    """Merge duplicates, drop facts whose date has passed, file 'misc' facts properly -
    what sleep does for a memory. Every change is written to memory/changes.jsonl
    (with the old text), so nothing is lost for good."""
    from mint.core import llm
    try:
        last = json.loads(TIDIED.read_text()).get("at", 0)
    except (OSError, ValueError):
        last = 0
    if not force and time.time() - last < TIDY_EVERY:
        return "Tidied recently."
    with _lock:
        bank = _load()
    pool = [b for b in bank if not b.get("pinned")]
    ROOT.mkdir(parents=True, exist_ok=True)
    TIDIED.write_text(json.dumps({"at": time.time()}))
    if len(pool) < 4:
        return "Too few memories to tidy."
    facts = "\n".join(f"{b['id']} | {b['group']} | {b.get('updated', b.get('created', ''))[:10]} | {b['text']}"
                      for b in pool[-300:])
    answer = llm.ask_json(_TIDY.format(today=time.strftime("%A %d %B %Y"), groups=", ".join(GROUPS), facts=facts))
    if not isinstance(answer, dict):
        return "Could not tidy (no answer)."
    done = []
    with _lock:
        bank = _load()
        by_id = {b["id"]: b for b in bank if not b.get("pinned")}
        for m in (answer.get("merge") or [])[:8]:
            ids = list(dict.fromkeys(i for i in m.get("ids") or [] if i in by_id))   # no id twice
            text = " ".join(str(m.get("text") or "").split())
            if len(ids) < 2 or not text or has_secret(text):
                continue
            keep, *drop = [by_id[i] for i in ids]
            _log_change("merge", {"kept": keep["id"], "before": [b["text"] for b in [keep, *drop]], "after": text})
            before = keep["text"]
            keep["text"], keep["updated"] = text, time.strftime("%Y-%m-%d %H:%M")
            _keep_history(keep, before)
            for b in drop:
                bank.remove(b)
                by_id.pop(b["id"], None)
            done.append(f"merged {len(ids)} into '{text[:60]}'")
        for e in (answer.get("expired") or [])[:8]:
            b = by_id.pop(str(e.get("id")), None)
            if b is not None:
                _log_change("expired", {"id": b["id"], "text": b["text"], "why": e.get("why", "")})
                bank.remove(b)
                done.append(f"dropped past '{b['text'][:60]}'")
        for r in (answer.get("regroup") or [])[:12]:
            b = by_id.get(str(r.get("id")))
            group = _group_name(str(r.get("group") or ""))
            if b is not None and b["group"] == "misc" and group in GROUPS and group not in PINNED_GROUPS:
                _log_change("regroup", {"id": b["id"], "from": b["group"], "to": group})
                b["group"] = group
                done.append(f"filed '{b['text'][:40]}' under {group}")
        if done:
            _save(bank)
    log.info("memory tidy: %s", "; ".join(done) or "nothing to do")
    return ("Tidied the memory: " + "; ".join(done)) if done else "The memory was already tidy."


def tidy_later(delay: float = 120.0) -> None:
    """In the background, a while after Mint starts, at most once a day."""
    def run():
        try:
            tidy()
        except Exception:
            log.exception("memory tidy failed")
    timer = threading.Timer(delay, run)
    timer.daemon = True
    timer.start()
