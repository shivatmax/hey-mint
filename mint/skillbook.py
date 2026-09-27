"""Skills: how-tos Mint has learned, kept as Markdown files by category.

    skills/
      apps/chatgpt/create-a-project.md
      apps/zcode/start-a-new-project.md
      coding/claude-code/resume-a-session.md
      _archive/...                     deleted skills, kept in case

Each file is readable and editable by hand:

    ---
    title: Create a project in ChatGPT
    when: The user wants a new ChatGPT project, or to start work in one
    apps: ChatGPT
    source: auto | user | mint
    created: 2026-09-24 02:40
    uses: 3
    wins: 3
    fails: 0
    ---
    ## Steps
    1. open_app ChatGPT (it opens on the last chat).
    2. click "Add new project" in the sidebar, under Projects.
    ## Notes
    - The prompt box takes type_text directly; no click needed.

Choosing a skill is Jev's job: given the task, it picks one skill from the
real list (or none) with a calibrated confidence, in about a second. Gemini
then follows the steps. Results feed back into the file (uses, wins, fails,
notes), and the list is ranked by how well each skill has worked.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from pathlib import Path

from . import config, jev

log = logging.getLogger("mint.skillbook")

ROOT = config.PROJECT_ROOT / "skills"
ARCHIVE = "_archive"
_lock = threading.RLock()

_SECRET = re.compile(
    r"(pass(word|code)?\s*[:=]|api[_ -]?key\s*[:=]|secret\s*[:=]|token\s*[:=]|\bsk-[A-Za-z0-9]{12,}"
    r"|\bAIza[0-9A-Za-z_\-]{20,}|\bapikey_[0-9a-f]{16,}|\bAQ\.[A-Za-z0-9_\-]{20,}|\b(?:\d[ -]?){13,19}\b)",
    re.IGNORECASE)


def has_secret(text: str) -> bool:
    """Skills and memories are plain files: never let a password or key in."""
    return bool(_SECRET.search(text or ""))


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:60] or "skill"


def clean_category(text: str) -> str:
    parts = [slug(p) for p in re.split(r"[/>\\]+", text or "") if slug(p)]
    return "/".join(parts[:3]) or "general"


# --- the file format ------------------------------------------------------------------

def _parse(path: Path) -> dict | None:
    try:
        raw = path.read_text()
    except OSError:
        return None
    meta, body = {}, raw
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
    if match:
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip():
                meta[key.strip()] = value.strip()
        body = match.group(2)
    rel = path.relative_to(ROOT)
    for key in ("uses", "wins", "fails"):
        try:
            meta[key] = int(meta.get(key, 0) or 0)
        except ValueError:
            meta[key] = 0
    return {"path": path, "name": path.stem, "category": "/".join(rel.parts[:-1]) or "general",
            "title": meta.get("title") or path.stem.replace("-", " "), "meta": meta, "body": body.strip()}


def _write(path: Path, meta: dict, body: str) -> None:
    order = ["title", "when", "apps", "source", "created", "updated", "last_used", "uses", "wins", "fails"]
    keys = order + [k for k in meta if k not in order]
    head = "\n".join(f"{k}: {meta[k]}" for k in keys if k in meta and meta[k] not in (None, ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{head}\n---\n{body.strip()}\n")


def _sections(body: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    current = "_intro"
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.*)$", line)
        if heading:
            current = heading.group(1).strip().lower()
            parts.setdefault(current, "")
            continue
        parts[current] = parts.get(current, "") + line + "\n"
    return {k: v.strip() for k, v in parts.items()}


def _body(steps: list[str] | str | None, notes: list[str] | str | None, extra: dict | None = None) -> str:
    def lines(value, numbered):
        if not value:
            return ""
        items = value if isinstance(value, list) else [l for l in str(value).splitlines() if l.strip()]
        items = [re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", str(i)).strip() for i in items if str(i).strip()]
        return "\n".join(f"{n}. {i}" if numbered else f"- {i}" for n, i in enumerate(items, 1))
    out = ""
    if steps:
        out += "## Steps\n" + lines(steps, True) + "\n\n"
    if notes:
        out += "## Notes\n" + lines(notes, False) + "\n\n"
    for heading, text in (extra or {}).items():
        if text:
            out += f"## {heading}\n{text}\n\n"
    return out.strip()


# --- reading ----------------------------------------------------------------------------

SEEDS = Path(__file__).resolve().parent / "seed_skills"
_seeded_once = False


def _seed() -> None:
    """Copy the starter skills in, once each. A seed is never copied over a
    learned skill of the same name, and one the user deleted stays deleted."""
    global _seeded_once
    if _seeded_once or not SEEDS.exists():
        return
    _seeded_once = True
    record = ROOT / ".seeded"
    try:
        done = set(record.read_text().split()) if record.exists() else set()
    except OSError:
        done = set()
    added = []
    for seed in SEEDS.rglob("*.md"):
        rel = str(seed.relative_to(SEEDS))
        target = ROOT / rel
        if rel in done or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seed.read_text())
        added.append(rel)
    if added:
        ROOT.mkdir(parents=True, exist_ok=True)
        record.write_text("\n".join(sorted(done | set(added))) + "\n")
        log.info("seeded %d starter skills", len(added))


def all_skills() -> list[dict]:
    _seed()
    if not ROOT.exists():
        return []
    found = []
    for path in sorted(ROOT.rglob("*.md")):
        if ARCHIVE in path.relative_to(ROOT).parts:
            continue
        skill = _parse(path)
        if skill:
            found.append(skill)
    return sorted(found, key=score, reverse=True)


def score(skill: dict) -> float:
    """Rank: skills that keep working rise; failing ones sink; new ones sit in the middle."""
    m = skill["meta"]
    wins, fails = m["wins"], m["fails"]
    rate = (wins + 1) / (wins + fails + 2)            # Laplace: new skill = 0.5
    return rate * 10 + min(m["uses"], 20) * 0.1


def rank(skill: dict) -> str:
    m = skill["meta"]
    if m["wins"] >= 3 and m["fails"] <= m["wins"] / 4:
        return "reliable"
    if m["fails"] > m["wins"]:
        return "shaky"
    return "new" if m["uses"] < 2 else "learning"


def categories() -> list[str]:
    return sorted({s["category"] for s in all_skills()})


def _describe(skill: dict) -> str:
    m = skill["meta"]
    apps = f" [apps: {m['apps']}]" if m.get("apps") else ""
    return f"{skill['title']} — use when: {m.get('when', '')}{apps} ({skill['category']})"[:300]


def index_text(limit: int = 40) -> str:
    skills = all_skills()
    if not skills:
        return ""
    lines = [f"- {s['category']}: {s['title']}" for s in skills[:limit]]
    more = f"\n- …and {len(skills) - limit} more (list_skills)" if len(skills) > limit else ""
    return "\n".join(lines) + more


def get(name: str, fuzzy: bool = True) -> dict | None:
    """By file name, exact title, or - failing those - Jev over the titles."""
    wanted = slug(name)
    skills = all_skills()
    for skill in skills:
        if skill["name"] == wanted or slug(skill["title"]) == wanted:
            return skill
    if not skills or not fuzzy:
        return None
    options = {str(i): _describe(s) for i, s in enumerate(skills[:250])}
    pick = jev.choose(name, options)
    return skills[int(pick.id)] if pick is not None and pick.sure else None


def for_app(app_name: str) -> list[dict]:
    wanted = app_name.lower().strip()
    if not wanted:
        return []
    return [s for s in all_skills()
            if wanted in (s["meta"].get("apps", "").lower()) or wanted in s["category"]]


# --- choosing (Jev) ----------------------------------------------------------------------

def find(task: str, app: str = "") -> tuple[dict | None, str]:
    """The skill for a task, chosen by Jev from the real list, or None."""
    skills = all_skills()
    if not skills:
        return None, "No skills saved yet."
    pool = skills
    if app:
        preferred = for_app(app)
        pool = preferred + [s for s in skills if s not in preferred]
    options = {str(i): _describe(s) for i, s in enumerate(pool[:250])}
    started = time.monotonic()
    pick = jev.choose(
        task, options, context={"app in front": app} if app else None, timeout=4.0,
        instructions=("A voice assistant that operates a Mac keeps a library of skills: learned "
                      "step-by-step how-tos. Which skill should it follow to do `request`? Pick a "
                      "skill only if it clearly covers this task (same app or site, same kind of "
                      "action). A skill for a particular app (ChatGPT, Slack, ZCode...) fits only when "
                      "the request names that app or is plainly about it: a general question, a web "
                      "search, reading or saving a file, or a menu command in the app in front is not a "
                      "task for another app's skill. Otherwise choose none."))
    took = time.monotonic() - started
    if pick is None:
        # Jev unreachable (it timed out now and then in testing): fall back to
        # word overlap, but only on a clear winner.
        best, why = _lexical(task + " " + app, pool)
        if best is not None:
            return best, why
        return None, "Could not reach Jev to choose a skill, and no skill clearly matches; carry on without one."
    if not pick.id:
        return None, f"No saved skill fits (Jev, {took:.1f}s)."
    if pick.confidence < 0.45:
        return None, f"No saved skill clearly fits (best guess '{pool[int(pick.id)]['title']}' at {pick.confidence:.2f})."
    return pool[int(pick.id)], f"Jev chose it ({pick.confidence:.2f} confident, {took:.1f}s)"


def instructions_for(skill: dict) -> str:
    m = skill["meta"]
    record = f"used {m['uses']} times, worked {m['wins']}, failed {m['fails']} - {rank(skill)}"
    return (f"SKILL '{skill['title']}' ({skill['category']}/{skill['name']}; {record}).\n"
            f"{skill['body']}\n"
            "Follow these steps, adapting if the screen differs. When done, call skill_result "
            f"with name '{skill['name']}', whether it worked, and anything you learned.")


def mark_used(skill: dict) -> None:
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        meta["uses"] = meta["uses"] + 1
        meta["last_used"] = time.strftime("%Y-%m-%d %H:%M")
        _write(skill["path"], meta, fresh["body"])


# --- writing ----------------------------------------------------------------------------

def create(title: str, when: str, steps, notes=None, category: str = "", apps: str = "",
           source: str = "mint") -> tuple[dict | None, str]:
    text = " ".join([title, when, str(steps), str(notes or ""), apps])
    if has_secret(text):
        return None, ("Refused: that looks like it contains a password, key or card number. "
                      "Skills are plain files; keep secrets out of them.")
    if not title.strip() or not steps:
        return None, "A skill needs a title and at least one step."
    category = clean_category(category) if category else _choose_category(title, when, apps)
    with _lock:
        existing = get(title, fuzzy=False)
        if existing is not None:
            return update(existing["name"], steps=steps, add_note=notes, when=when, apps=apps,
                          source=source)
        path = ROOT / category / f"{slug(title)}.md"
        n = 2
        while path.exists():
            path = ROOT / category / f"{slug(title)}-{n}.md"
            n += 1
        now = time.strftime("%Y-%m-%d %H:%M")
        meta = {"title": title.strip(), "when": when.strip(), "apps": apps.strip(), "source": source,
                "created": now, "uses": 0, "wins": 0, "fails": 0}
        _write(path, meta, _body(steps, notes))
    skill = _parse(path)
    return skill, f"Saved skill '{title}' in {category}."


def update(name: str, steps=None, add_note=None, when: str = "", title: str = "", apps: str = "",
           source: str = "", replace_notes=None) -> tuple[dict | None, str]:
    if has_secret(" ".join(str(x) for x in (steps, add_note, when, title, apps) if x)):
        return None, "Refused: that looks like it contains a password, key or card number."
    skill = get(name)
    if skill is None:
        return None, f"No skill called '{name}'."
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        sections = _sections(fresh["body"])
        old_steps = sections.pop("steps", "")
        old_notes = sections.pop("notes", "")
        sections.pop("_intro", None)
        notes = [l for l in old_notes.splitlines() if l.strip()]
        if replace_notes is not None:
            # A consolidated list from the learner: appending forever let stale
            # workarounds pile up (one skill reached 12 notes, some contradictory).
            notes = [f"- {str(n).strip()}" for n in replace_notes if str(n).strip()][:8]
            add_note = None
        for note in ([add_note] if isinstance(add_note, str) else (add_note or [])):
            note = str(note).strip()
            if note and note not in old_notes:
                notes.append(f"- {note} ({time.strftime('%Y-%m-%d')})")
        new_steps = steps if steps else [l for l in old_steps.splitlines() if l.strip()]
        if when:
            meta["when"] = when
        if title:
            meta["title"] = title
        if apps:
            meta["apps"] = apps
        if source and source not in meta.get("source", ""):
            meta["source"] = f"{meta.get('source', '')}+{source}".strip("+")
        meta["updated"] = time.strftime("%Y-%m-%d %H:%M")
        _write(skill["path"], meta, _body(new_steps, notes, {k.title(): v for k, v in sections.items()}))
    return _parse(skill["path"]), f"Updated skill '{meta['title']}'."


def record_result(name: str, worked: bool, note: str = "") -> str:
    skill = get(name)
    if skill is None:
        return f"No skill called '{name}'."
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        meta["wins" if worked else "fails"] += 1
        _write(skill["path"], meta, fresh["body"])
    if note:
        update(skill["name"], add_note=("Worked: " if worked else "Failed: ") + note)
    return f"Recorded: '{skill['title']}' {'worked' if worked else 'failed'}."


def archive(name: str) -> str:
    skill = get(name)
    if skill is None:
        return f"No skill called '{name}'."
    target = ROOT / ARCHIVE / skill["category"] / skill["path"].name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill["path"]), str(target))
    return f"Removed skill '{skill['title']}' (kept a copy in skills/{ARCHIVE})."


def listing(category: str = "") -> str:
    skills = [s for s in all_skills() if not category or s["category"].startswith(clean_category(category))]
    if not skills:
        return "No skills saved yet." if not category else f"No skills under '{category}'."
    lines, last = [], None
    for skill in sorted(skills, key=lambda s: (s["category"], -score(s))):
        if skill["category"] != last:
            lines.append(f"{skill['category']}:")
            last = skill["category"]
        m = skill["meta"]
        lines.append(f"  - {skill['title']} [{rank(skill)}; {m['wins']}/{m['uses']} worked] (name: {skill['name']})")
    return "\n".join(lines)


def _choose_category(title: str, when: str, apps: str) -> str:
    """Jev files a new skill under an existing category when one fits."""
    existing = categories()
    if apps:
        guess = f"apps/{slug(apps.split(',')[0])}"
    else:
        guess = "general"
    if not existing:
        return guess
    options = {str(i): c for i, c in enumerate(existing[:250])}
    pick = jev.choose(f"{title}. {when}. Apps: {apps}", options,
                      instructions=("Skills are filed in category folders. Which existing folder "
                                    "does this new skill belong in? Choose none if none fits well."))
    if pick is not None and pick.sure:
        return existing[int(pick.id)]
    return guess


_STOP = {"the", "a", "an", "in", "on", "to", "and", "of", "for", "my", "me", "it", "app", "open",
         "please", "with", "into", "then", "new", "this", "that", "use", "using"}


def _lexical(text: str, pool: list[dict]) -> tuple[dict | None, str]:
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2}
    if not words:
        return None, ""
    scored = []
    for skill in pool:
        blob = " ".join([skill["title"], skill["meta"].get("when", ""), skill["meta"].get("apps", ""),
                         skill["category"]]).lower()
        have = {w for w in re.findall(r"[a-z0-9]+", blob)}
        scored.append((len(words & have) / len(words), skill))
    scored.sort(key=lambda x: -x[0])
    top = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if top[0] >= 0.5 and top[0] - runner >= 0.2:
        return top[1], f"word match ({top[0]:.2f}; Jev was unreachable)"
    return None, ""


# --- direct edits (the Skills & Memory window) ----------------------------------------------

def save_raw(path: Path, title: str, when: str, apps: str, category: str, body: str) -> tuple[Path, str]:
    """Save a skill exactly as edited by hand. Moves the file if the category or
    title changed. Returns (new path, message)."""
    text = " ".join([title, when, apps, body])
    if has_secret(text):
        return path, "Refused: that looks like it contains a password, key or card number."
    if not title.strip():
        return path, "A skill needs a title."
    with _lock:
        fresh = _parse(path) or {"meta": {"uses": 0, "wins": 0, "fails": 0}, "body": ""}
        meta = dict(fresh["meta"])
        meta.update(title=title.strip(), when=" ".join(when.split()), apps=apps.strip(),
                    updated=time.strftime("%Y-%m-%d %H:%M"))
        if "edited" not in meta.get("source", ""):
            meta["source"] = f"{meta.get('source', '')}+edited".strip("+")
        target = ROOT / clean_category(category) / f"{slug(title)}.md"
        if target != path and target.exists():
            return path, f"A skill called '{title}' already exists in {clean_category(category)}."
        _write(target, meta, body)
        if target != path and path.exists():
            path.unlink()
    return target, "Saved."


def new_blank(category: str = "general") -> Path:
    """A fresh skill to fill in, named so it cannot clash."""
    n = 1
    while True:
        title = "New skill" if n == 1 else f"New skill {n}"
        path = ROOT / clean_category(category) / f"{slug(title)}.md"
        if not path.exists():
            break
        n += 1
    meta = {"title": title, "when": "Describe when to use it, in terms of what the user asks.", "apps": "",
            "source": "user", "created": time.strftime("%Y-%m-%d %H:%M"), "uses": 0, "wins": 0, "fails": 0}
    _write(path, meta, _body(["First step (name the tool and the exact on-screen label)"], ["Gotchas go here"]))
    return path
