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
        chosen = _lexical(query, pool, limit)
        log.info("recall: Jev unreachable, word match gave %d", len(chosen))
    else:
        scored = sorted(((jev.yes(answers, b["id"]), b) for b in pool), key=lambda x: -x[0])
        chosen = [b for p, b in scored if p >= RELEVANT][:limit]
    log.info("recall %r -> %d blocks in %.1fs", query[:60], len(chosen), time.monotonic() - started)
    if chosen:
        _touch([b["id"] for b in chosen])
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
    return "\n".join(f"- ({b['group']}) {b['text']}" for b in found)


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
    answers = jev.ask({"request": text}, questions, timeout=6.0, retries=1)
    if answers is None:
        return group or "misc", None
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


def add(text: str, group: str = "", pinned: bool | None = None, source: str = "user") -> str:
    text = " ".join(str(text).split())
    if not text:
        return "Nothing to remember."
    if has_secret(text):
        return ("Refused: that looks like a password, key or card number. Those are never stored; "
                "use a password manager.")
    with _lock:
        bank = _load()
        if any(b["text"].lower() == text.lower() for b in bank):
            return "Already remembered."
        group, old = _file(text, _group_name(group) if group else "", bank)
        if pinned is None:
            pinned = group in PINNED_GROUPS
        if old is not None:
            before = old["text"]
            old.update(text=text, updated=time.strftime("%Y-%m-%d %H:%M"), source=source,
                       pinned=bool(pinned or old.get("pinned")))
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
    ranked, _ = jev.rank(what, options, "Which remembered fact is `request` talking about?", timeout=6.0)
    if ranked and ranked[0][1] >= 0.5:
        return next(b for b in bank if b["id"] == ranked[0][0]), ""
    listing_ = "; ".join(b["text"] for b in bank[-12:])
    return None, f"Not sure which memory '{what}' means. Some of what is remembered: {listing_}"


def update(what: str, new_text: str = "", fixed: bool | None = None, group: str = "") -> str:
    if new_text and has_secret(new_text):
        return "Refused: that looks like a password, key or card number."
    with _lock:
        bank = _load()
        if not bank:
            return "Nothing is remembered yet."
        block, why = _find(what, bank)
        if block is None:
            return why
        before = block["text"]
        if new_text:
            block["text"] = " ".join(new_text.split())
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
        bank = _load()
        if not bank:
            return "There is nothing remembered to forget."
        block, why = _find(what, bank)
        if block is None:
            return why
        bank.remove(block)
        _save(bank)
    return f"Forgot: {block['text']}"


def clear() -> None:
    with _lock:
        for path in (BANK, VIEW):
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
