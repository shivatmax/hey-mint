"""Skills: how-tos Mint has learned, kept as Markdown files by category.

    skills/
      apps/chatgpt/create-a-project.md
      apps/zcode/start-a-new-project.md
      coding/claude-code/resume-a-session.md
      coding/claude-code/resume-a-session/    optional support files:
          references/*.md                       depth needed only sometimes
          scripts/*                             AppleScript, Shortcut names (plain text)
      _archive/...                     deleted skills, kept in case
      .ledger.jsonl, .ledger/blobs/    every change, with the text before and after

Each file is readable and editable by hand:

    ---
    title: Create a project in ChatGPT
    when: The user wants a new ChatGPT project, or to start work in one
    apps: ChatGPT
    created_by: auto | mint | user | seed | teach
    pinned: yes                      (optional: nothing automatic may change it)
    state: stale                     (optional: not used for a month; listed last)
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

Who may change what (created_by): the review after a task (learner.py) and the
curator edit only skills Mint made (auto, mint). Skills the user wrote, the
starter set and lessons get at most a "Suggested" note, unless the user asked;
a pinned skill is never changed by anything automatic. Every write goes through
_commit, which records it in the ledger (skill_ledger.py), so any change can
be undone (rollback).
"""

from __future__ import annotations

import functools
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from mint.core import config
from mint.core import jev
from mint.knowledge import skill_ledger
from mint.core import untrusted

log = logging.getLogger("mint.knowledge.skills")

ROOT = config.PROJECT_ROOT / "skills"
ARCHIVE = "_archive"
SUPPORT_KINDS = ("references", "scripts")
_lock = threading.RLock()

CREATORS = ("user", "mint", "auto", "seed", "teach")
AUTO_EDITABLE = {"auto", "mint"}             # what automatic actors may change in full
AUTOMATIC = {"review", "curator", "job"}     # actors with nobody watching
STALE_DAYS = 30                              # unused this long -> "stale": listed last, still found
ARCHIVE_DAYS = 90                            # stale, unused this long and made by the review -> archived
CURATE_EVERY = 20 * 3600
MAX_SUGGESTED = 4

_write_listeners: list = []


def on_write(callback) -> None:
    """callback(action, skill_name, actor) after any change to a skill's text."""
    _write_listeners.append(callback)

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
    migrate = meta.get("created_by") not in CREATORS
    if migrate:
        meta["created_by"] = _creator(meta.get("source", ""), str(rel))
    return {"path": path, "name": path.stem, "category": "/".join(rel.parts[:-1]) or "general",
            "title": meta.get("title") or path.stem.replace("-", " "), "meta": meta, "body": body.strip(),
            "migrate": migrate}


def _creator(source: str, rel: str = "") -> str:
    """created_by from the older `source` trail ('seed+auto+edited' -> seed: who made it first)."""
    first = (source or "").split("+")[0].strip().lower()
    mapped = {"taught": "teach", "teach": "teach", "user": "user", "seed": "seed", "auto": "auto",
              "mint": "mint"}.get(first)
    if mapped:
        return mapped
    try:
        seeded = set((ROOT / ".seeded").read_text().split())
    except OSError:
        seeded = set()
    return "seed" if rel in seeded else "mint"


def _render(meta: dict, body: str) -> str:
    order = ["title", "when", "apps", "created_by", "pinned", "state", "source", "created", "updated", "last_used",
             "uses", "wins", "fails"]
    keys = order + [k for k in meta if k not in order]
    head = "\n".join(f"{k}: {meta[k]}" for k in keys if k in meta and meta[k] not in (None, ""))
    return f"---\n{head}\n---\n{body.strip()}\n"


def _write(path: Path, meta: dict, body: str) -> None:
    """Counters and state only (uses, wins, last_used, stale): not a change worth a ledger entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(meta, body))
    held = _snapshot["skills"]
    if held is not None:                            # a use or a counter: update the big library's list in place
        fresh = _parse(path)
        spot = next((i for i, s in enumerate(held) if s["path"] == path), None)
        if fresh is None or spot is None:
            _snapshot["skills"] = None
        else:
            held[spot] = fresh


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _commit(path: Path, meta: dict, body: str, action: str, actor: str, reason: str = "") -> None:
    """Write a change to a skill's text, recorded in the ledger with the text before and after."""
    before = _read(path)
    _write(path, meta, body)
    skill_ledger.record(action, path.stem, _rel(path), before, _read(path), actor, reason,
                        title=str(meta.get("title", "")))
    _wrote(action, path.stem, actor)


def _wrote(action: str, name: str, actor: str) -> None:
    _snapshot["skills"] = None                      # a skill's text changed: list the library again
    for callback in list(_write_listeners):
        try:
            callback(action, name, actor)
        except Exception:
            log.exception("on_write callback failed")


def is_pinned(skill: dict) -> bool:
    return str(skill["meta"].get("pinned", "")).lower() in ("yes", "true", "1")


def is_stale(skill: dict) -> bool:
    return str(skill["meta"].get("state", "")).lower() == "stale"


def edit_guard(skill: dict, actor: str, asked: bool = False) -> str:
    """May `actor` change this skill? '' = yes, 'suggest' = only add a Suggested note, else why not.
    The user (the window, an explicit request) may change anything; nobody else a pinned skill;
    automatic actors only what Mint made, unless the user asked."""
    if actor == "user":
        return ""
    if is_pinned(skill):
        return (f"'{skill['title']}' is pinned: only the user changes it (they can unpin it in Skills & "
                "Memory).")
    if actor in AUTOMATIC and skill["meta"].get("created_by") not in AUTO_EDITABLE and not asked:
        return "suggest"
    return ""


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

SEEDS = Path(__file__).resolve().parents[1] / "resources" / "skills"
_seeded_once = False


def _seed_body(path: Path) -> str:
    match = re.match(r"^---\n.*?\n---\n?(.*)$", path.read_text(), re.S)
    return (match.group(1) if match else "").strip()


def _untouched_seed(target: Path, seed: Path) -> bool:
    try:
        parsed = _parse(target)
        body = _seed_body(seed)
    except Exception:
        return False
    if parsed is None or not body or parsed["body"].strip() == body:
        return False
    meta = parsed["meta"]
    return meta.get("created_by") == "seed" and not int(meta.get("uses") or 0) and "updated" not in meta


def _refresh_seed(target: Path, seed: Path) -> None:
    try:
        target.write_text(seed.read_text())
        parsed = _parse(target)
        if parsed is not None:
            _write(target, dict(parsed["meta"], created_by="seed"), parsed["body"])
        log.info("refreshed starter skill %s", target.name)
    except OSError:
        log.debug("refresh %s", target, exc_info=True)


def _seed() -> None:
    """Copy the starter skills in, once each. A seed is never copied over a
    learned skill of the same name, and one the user deleted stays deleted."""
    global _seeded_once
    if _seeded_once:
        return
    _seeded_once = True
    record = ROOT / ".seeded"
    try:
        done = set(record.read_text().split()) if record.exists() else set()
    except OSError:
        done = set()
    _retire_dropped_seeds(done)
    added = []
    for seed in (SEEDS.rglob("*.md") if SEEDS.exists() else ()):
        rel = str(seed.relative_to(SEEDS))
        target = ROOT / rel
        if rel in done and target.exists() and _untouched_seed(target, seed):
            # A newer starter skill replaces the old copy as long as it was never used or edited: built-in
            # know-how improves with each version for everyone, not just new installs.
            _refresh_seed(target, seed)
            continue
        if rel in done or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seed.read_text())
        parsed = _parse(target)
        if parsed is not None:
            meta = dict(parsed["meta"], created_by="seed")
            _write(target, meta, parsed["body"])
            skill_ledger.record("create", target.stem, rel, None, _read(target), "seed", "starter skill",
                                title=parsed["title"])
        added.append(rel)
    if added:
        ROOT.mkdir(parents=True, exist_ok=True)
        record.write_text("\n".join(sorted(done | set(added))) + "\n")
        log.info("seeded %d starter skills", len(added))


def _retire_dropped_seeds(done: set[str]) -> None:
    """A starter skill Mint no longer ships (9 Oct: the basics - clicking, typing, files, the web, chat apps, Mint's
    own settings - became built-in how-to, guides.py) leaves the library if it was never used or edited; a used
    or changed copy is the user's now and stays."""
    for rel in sorted(done):
        target = ROOT / rel
        if (SEEDS / rel).exists() or not target.exists():
            continue
        parsed = _parse(target)
        if parsed is None:
            continue
        meta = parsed["meta"]
        if (meta.get("created_by") == "seed" and str(meta.get("source", "seed")) == "seed"
                and not int(meta.get("uses") or 0) and "updated" not in meta):
            try:
                archive(parsed["name"], actor="seed", reason="no longer a starter skill (now built in)", skill=parsed)
                log.info("retired starter skill %s", rel)
            except Exception:
                log.debug("retire %s", rel, exc_info=True)


def _is_support(path: Path) -> bool:
    """A file inside a skill's support folder (skills/<cat>/<slug>/references/...), or the ledger."""
    rel = path.relative_to(ROOT)
    if any(part.startswith(".") for part in rel.parts):
        return True
    for parent in rel.parents:
        if str(parent) != "." and (ROOT / parent).with_suffix(".md").is_file():
            return True
    return False


def _migrate(skill: dict) -> None:
    """Older files have only `source`: write the created_by derived from it, once (not a ledger change)."""
    if not skill.pop("migrate", False):
        return
    with _lock:
        _write(skill["path"], skill["meta"], skill["body"])


_parsed: dict[Path, tuple[float, dict | None]] = {}     # path -> (mtime, parsed): a big library is parsed once
# A big library (thousands of learned skills) is listed again at most every SNAPSHOT_FOR seconds - or at once
# after any change made here (_wrote); counters (_write) are updated in place. A small one is always re-listed.
_snapshot: dict = {"root": None, "at": 0.0, "skills": None}
SNAPSHOT_FROM, SNAPSHOT_FOR = 2000, 300.0


def all_skills() -> list[dict]:
    _seed()
    if not ROOT.exists():
        return []
    held = _snapshot["skills"]
    if held is not None and _snapshot["root"] == ROOT and time.monotonic() - _snapshot["at"] < SNAPSHOT_FOR:
        return [dict(s, meta=dict(s["meta"])) for s in held]
    found, seen = [], set()
    paths = sorted(ROOT.rglob("*.md"))
    every = set(paths)
    for path in paths:
        rel = path.relative_to(ROOT)
        if (ARCHIVE in rel.parts or any(part.startswith(".") for part in rel.parts)
                or any(str(up) != "." and (ROOT / up).with_suffix(".md") in every for up in rel.parents)):
            continue                                # archived, hidden, or a support file (as _is_support)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        seen.add(path)
        cached = _parsed.get(path)
        if cached is None or cached[0] != mtime:
            skill = _parse(path)
            if skill:
                try:
                    _migrate(skill)
                except OSError:
                    log.debug("could not migrate %s", path, exc_info=True)
            cached = _parsed[path] = (mtime, skill)
        if cached[1]:
            found.append(dict(cached[1], meta=dict(cached[1]["meta"])))
    for gone in set(_parsed) - seen:
        _parsed.pop(gone, None)
    found.sort(key=score, reverse=True)
    if len(found) >= SNAPSHOT_FROM:
        _snapshot.update(root=ROOT, at=time.monotonic(), skills=[dict(s, meta=dict(s["meta"])) for s in found])
    else:
        _snapshot["skills"] = None
    return found


def score(skill: dict) -> float:
    """Rank: skills that keep working rise; failing ones sink; new ones sit in the middle; stale ones
    (unused for a month) sink below every active one."""
    m = skill["meta"]
    wins, fails = m["wins"], m["fails"]
    rate = (wins + 1) / (wins + fails + 2)            # Laplace: new skill = 0.5
    return rate * 10 + min(m["uses"], 20) * 0.1 - (20 if is_stale(skill) else 0)


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


INDEX_LINE = 60          # characters of "title — when" per skill in the prompt's index
_WHEN_BOILERPLATE = re.compile(r"^(use (it|this) )?((when(ever)?|if) )?((the user|i|you|someone) "
                               r"(wants?|asks?|needs?|would like|says?)( you| mint| me)?( to| for)?\s*)?", re.I)


def index_line(skill: dict, width: int = INDEX_LINE) -> str:
    """'Title — when to use it', the title whole (it is what find_skill takes), the rest cut to fit."""
    title = " ".join(skill["title"].split())
    when = " ".join(str(skill["meta"].get("when", "")).split())
    when = _WHEN_BOILERPLATE.sub("", when)               # "When the user wants to export..." -> "export..."
    room = width - len(title) - 3
    if not when or room < 12:
        return title
    if len(when) > room:
        when = when[:room - 1].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return f"{title} — {when}"


def index_text(max_chars: int = 3500) -> str:
    """Every skill, one short line each, for the system prompt: active ones by rank, stale ones last.
    Past max_chars the rest is counted, not listed."""
    skills = all_skills()
    if not skills:
        return ""
    ordered = [s for s in skills if not is_stale(s)] + [s for s in skills if is_stale(s)]
    lines, used = [], 0
    for n, skill in enumerate(ordered):
        line = f"- {index_line(skill)}" + (" (stale)" if is_stale(skill) else "")
        if used + len(line) + 1 > max_chars:
            lines.append(f"- …and {len(ordered) - n} more (list_skills)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


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


# One app, several names: what macOS calls it, what people say, what a skill was saved under.
APP_ALIASES = {
    "code": "visual studio code", "vs code": "visual studio code", "vscode": "visual studio code",
    "visual studio code": "visual studio code", "claude": "claude", "claude code": "claude code",
    "google chrome": "chrome", "chrome": "chrome", "microsoft word": "word", "microsoft excel": "excel",
    "microsoft powerpoint": "powerpoint", "microsoft outlook": "outlook", "microsoft teams": "teams",
    "chatgpt": "chatgpt", "chat gpt": "chatgpt", "iterm2": "iterm", "iterm": "iterm",
}


# Words some skills list under "apps" that are not apps a request would be "about".
_NOT_APPS = {"system", "macos", "mac", "files", "file system", "clipboard", "connector", "video editor", "general",
             "web", "browser", "terminal commands"}


def app_key(name: str) -> str:
    """'Code', 'VS Code', 'Visual Studio Code' -> 'visual studio code'."""
    name = " ".join(re.findall(r"[a-z0-9]+", (name or "").lower().removesuffix(".app")))
    return APP_ALIASES.get(name, name)


def skill_apps(skill: dict) -> set[str]:
    """The apps a skill is for (canonical names); empty for a general skill."""
    return set(_apps_of(skill["meta"].get("apps", "") or "", skill["category"]))


@functools.lru_cache(maxsize=4096)
def _apps_of(raw: str, category: str) -> frozenset[str]:
    apps = {app_key(a) for a in re.split(r"[,;/]+", raw) if app_key(a)}
    parts = category.split("/")
    if len(parts) >= 2 and parts[0] in ("apps", "browser") and parts[1] != "general":
        apps.add(app_key(parts[1].replace("-", " ")))
    return frozenset(apps)


def for_app(app_name: str) -> list[dict]:
    """Skills for this app. Whole names only: a substring match put 'Claude Code' skills on
    VS Code (macOS calls it 'Code') - 1 Oct, 'Manage tasks in Data Labeling Portal' for
    'download the Docker extension in VS Code'."""
    wanted = app_key(app_name)
    if not wanted:
        return []
    return [s for s in all_skills() if wanted in skill_apps(s)]


def mentioned_apps(text: str, skills: list[dict] | None = None) -> set[str]:
    """Which of the apps that skills are saved for (or known aliases) the text names, as words."""
    words = " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "
    names = set(APP_ALIASES)
    for skill in skills if skills is not None else all_skills():
        names |= skill_apps(skill)
        names |= {" ".join(re.findall(r"[a-z0-9]+", a.lower())) for a in
                  re.split(r"[,;/]+", skill["meta"].get("apps", "") or "") if a.strip()}
    found = {app_key(n) for n in names if n and n not in _NOT_APPS and f" {n} " in words}
    # "claude code" mentioned is not also "code" (VS Code)
    if "claude code" in found and not re.search(r"\b(vs ?code|visual studio)\b", words):
        found.discard("visual studio code")
    return found


def app_conflict(skill: dict, task: str, app: str = "", skills: list[dict] | None = None,
                 use_front: bool = True, named: set[str] | None = None) -> str:
    """Why `skill` is for another app than this task, or ''. A general skill (no apps) never
    conflicts. The task's app is the one it names, else (use_front) the app in front."""
    mine = skill_apps(skill)
    if not mine:
        return ""
    if named is None:
        named = mentioned_apps(task, skills)
    target = named or ({app_key(app)} if use_front and app_key(app) else set())
    if not target or mine & target:
        return ""
    return (f"it is for {', '.join(sorted(mine))}, and this task is about "
            f"{', '.join(sorted(target))}")


# --- choosing (Jev) ----------------------------------------------------------------------

def find(task: str, app: str = "") -> tuple[dict | None, str]:
    """The skill for a task, chosen by Jev from the real list, or None."""
    skills = all_skills()
    if not skills:
        return None, "No skills saved yet."
    pool = skills
    if app:
        wanted = app_key(app)
        preferred = [s for s in skills if wanted and wanted in skill_apps(s)]
        first = {id(s) for s in preferred}
        pool = preferred + [s for s in skills if id(s) not in first]
    named = mentioned_apps(task, skills)            # once: it walks the whole library
    if len(pool) > SHORTLIST:
        # A big library (thousands of learned skills): the chooser sees the best word matches, not the first N.
        pool = _shortlist(task, pool, app)
    options = {str(i): _describe(s) for i, s in enumerate(pool)}
    started = time.monotonic()
    if not jev.available():
        return _gemini_find(task, app, pool, skills, options, started, named)
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
        best, why = _lexical(task, [s for s in pool if not app_conflict(s, task, app, skills, named=named)])
        if best is not None:
            return best, why
        return None, "Could not reach Jev to choose a skill, and no skill clearly matches; carry on without one."
    if not pick.id:
        return None, f"No saved skill fits (Jev, {took:.1f}s)."
    chosen = pool[int(pick.id)]
    if pick.confidence < 0.45:
        return None, f"No saved skill clearly fits (best guess '{chosen['title']}' at {pick.confidence:.2f})."
    # The app the request names must be the skill's; the app merely in front counts only when Jev
    # is not sure ("manage my labeling tasks" may well be asked with Chrome in front).
    clash = app_conflict(chosen, task, app, skills, use_front=pick.confidence < 0.75, named=named)
    if clash:
        return None, f"No saved skill fits ('{chosen['title']}' came closest, but {clash})."
    return chosen, f"Jev chose it ({pick.confidence:.2f} confident, {took:.1f}s)"


SHORTLIST = 60
_CHOOSE = ("A voice assistant that operates a Mac keeps a library of skills: learned step-by-step how-tos. Which "
           "skill should it follow to do `request`? Pick a skill only if it clearly covers this task (same app or "
           "site, same kind of action). A skill for a particular app (ChatGPT, Slack, ZCode...) fits only when the "
           "request names that app or is plainly about it. Otherwise choose none.")


def _shortlist(task: str, pool: list[dict], app: str = "", size: int = SHORTLIST) -> list[dict]:
    """The `size` skills sharing the most (rarer) words with the task - title and `when` count most, the app
    named or in front adds - in a stable order (the pool's own, best first, for ties)."""
    words = {w for w in re.findall(r"[a-z0-9]+", f"{task} {app}".lower()) if w not in _STOP and len(w) > 2}
    if not words:
        return pool[:size]
    docs = []
    for skill in pool:
        strong = set(re.findall(r"[a-z0-9]+", f"{skill['title']} {skill['meta'].get('when', '')}".lower()))
        weak = set(re.findall(r"[a-z0-9]+", f"{skill['category']} {' '.join(skill_apps(skill))}".lower()))
        docs.append((strong, weak))
    have = {w: sum(1 for strong, weak in docs if w in strong or w in weak) for w in words}
    total = len(docs)
    scores = []
    for i, (strong, weak) in enumerate(docs):
        score = sum((1.0 if w in strong else 0.5) * (1 + total / (1 + have[w])) ** 0.5
                    for w in words if w in strong or w in weak)
        scores.append((-score, i))
    scores.sort()
    return [pool[i] for _, i in scores[:size]]


def _gemini_find(task: str, app: str, pool: list[dict], skills: list[dict], options: dict[str, str],
                 started: float, named: set[str] | None = None) -> tuple[dict | None, str]:
    """No TypeSafe key (most users): a cheap Gemini model chooses from the same shortlist; word overlap when it
    can't be reached."""
    from mint.core import llm
    listing = "\n".join(f"{k}: {v}" for k, v in options.items())
    answer = None

    def ask():
        from google.genai import types
        return llm.client().models.generate_content(
            model=llm.LITE[0], contents=(f"{_CHOOSE}\n\nSkills:\n{listing}\n\nrequest: {task}"
                                         + (f"\napp in front: {app}" if app else "")
                                         + '\n\nAnswer JSON only: {"id": "<skill id>" or "none"}'),
            config=types.GenerateContentConfig(
                temperature=0, response_mime_type="application/json", max_output_tokens=40,
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    try:
        from concurrent.futures import ThreadPoolExecutor
        runner = ThreadPoolExecutor(1)                # at most 5 s, like Jev's 4: then the word match decides
        try:
            reply = runner.submit(ask).result(timeout=5.0)
        finally:
            runner.shutdown(wait=False)
        answer = llm.parse_json(str(reply.text or "{}"))
    except Exception as error:
        log.info("skill choice by Gemini failed: %s", str(error)[:120])
    took = time.monotonic() - started
    chosen_id = str((answer or {}).get("id", "") if isinstance(answer, dict) else "")
    if answer is None:
        best, why = _lexical(task, [s for s in pool if not app_conflict(s, task, app, skills, named=named)])
        return (best, why) if best is not None else (None, "No skill clearly matches; carry on without one.")
    if chosen_id not in options:
        return None, f"No saved skill fits (Gemini, {took:.1f}s)."
    chosen = pool[int(chosen_id)]
    clash = app_conflict(chosen, task, app, skills, named=named)
    if clash:
        return None, f"No saved skill fits ('{chosen['title']}' came closest, but {clash})."
    return chosen, f"Gemini chose it ({took:.1f}s)"


def instructions_for(skill: dict) -> str:
    m = skill["meta"]
    record = f"used {m['uses']} times, worked {m['wins']}, failed {m['fails']} - {rank(skill)}"
    files = support_files(skill)
    extra = (f"\nSupport files (find_skill with name '{skill['name']}' and file=<path> reads one): "
             + ", ".join(files[:20])) if files else ""
    why = untrusted.blocked(f"{skill['title']} {m.get('when', '')} {skill['body']}", "skill")
    if why:           # kept on disk for the user to fix or delete in Skills & Memory; never given to the model
        return f"SKILL '{skill['title']}' ({skill['category']}/{skill['name']}): [BLOCKED: {why}]. Do the task without it."
    return (f"SKILL '{skill['title']}' ({skill['category']}/{skill['name']}; {record}).\n"
            f"{skill['body']}{extra}\n"
            "Follow these steps, adapting if the screen differs. When done, call skill_result "
            f"with name '{skill['name']}', whether it worked, and anything you learned.")


def mark_used(skill: dict) -> None:
    """A use also brings a stale skill back."""
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        meta["uses"] = meta["uses"] + 1
        meta["last_used"] = time.strftime("%Y-%m-%d %H:%M")
        meta.pop("state", None)
        _write(skill["path"], meta, fresh["body"])


# --- support files: skills/<category>/<name>/references/..., scripts/... --------------------------

def support_dir(skill: dict) -> Path:
    return skill["path"].with_suffix("")


def support_files(skill: dict) -> list[str]:
    """'references/x.md', 'scripts/y.applescript', ... (text files only)."""
    base = support_dir(skill)
    found = []
    for kind in SUPPORT_KINDS:
        folder = base / kind
        if folder.is_dir():
            found += [str(p.relative_to(base)) for p in sorted(folder.rglob("*"))
                      if p.is_file() and not p.name.startswith(".")]
    return found


def _support_path(skill: dict, rel: str) -> Path | None:
    """The support file `rel` inside this skill's folder, or None if it points anywhere else."""
    rel = str(rel or "").strip().lstrip("/")
    if not rel or rel.split("/")[0] not in SUPPORT_KINDS:
        return None
    base = support_dir(skill).resolve()
    target = (support_dir(skill) / rel).resolve()
    return target if base in target.parents else None


def read_support(skill: dict, rel: str, max_chars: int = 8000) -> str:
    target = _support_path(skill, rel)
    if target is None:
        return (f"No support file '{rel}' for '{skill['title']}'. It has: "
                + (", ".join(support_files(skill)) or "none") + ".")
    try:
        text = target.read_text(errors="replace")
    except OSError:
        return f"No support file '{rel}' for '{skill['title']}'. It has: {', '.join(support_files(skill)) or 'none'}."
    why = untrusted.blocked(text, "skill")
    if why:
        return f"{skill['title']} - {rel}: [BLOCKED: {why}]. The user can fix or delete it in Finder."
    more = f"\n…(cut at {max_chars} characters)" if len(text) > max_chars else ""
    return f"{skill['title']} - {rel}:\n{text[:max_chars]}{more}"


def write_support(name: str, rel: str, text: str, actor: str = "mint", reason: str = "",
                  asked: bool = False) -> str:
    """Add or replace one support file (plain text; nothing here is ever run by Mint itself)."""
    skill = get(name, fuzzy=False)
    if skill is None:
        return f"No skill called '{name}'."
    refused = edit_guard(skill, actor, asked)
    if refused:
        return "Refused: " + ("support files of a skill Mint did not make are the user's." if refused == "suggest"
                              else refused)
    target = _support_path(skill, rel)
    if target is None:
        return f"Refused: support files go under {' or '.join(k + '/' for k in SUPPORT_KINDS)} in the skill's folder."
    if has_secret(text):
        return "Refused: that looks like it contains a password, key or card number."
    if untrusted.refusal(text, "skill"):
        return untrusted.refusal(text, "skill")
    text = str(text or "").strip()[:20000] + "\n"
    with _lock:
        before = _read(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        skill_ledger.record("update" if before is not None else "create", skill["name"], _rel(target), before,
                            text, actor, reason, title=skill["title"], file=rel)
    _wrote("support", skill["name"], actor)
    return f"Saved {rel} for '{skill['title']}'."


# --- lint: advisory checks on a skill's shape ---------------------------------------------------

LINT_MAX_BODY = 6000
LINT_MAX_NOTES = 8
MONTH = (r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|"
         r"nov(ember)?|dec(ember)?)")
_DATED = re.compile(r"\b(20\d\d-\d\d-\d\d|\d{1,2} " + MONTH + r"\b|" + MONTH + r" \d{1,2}\b|yesterday|today|"
                    r"this morning|last (time|week|night)|in testing|at \d{1,2}:\d\d)", re.I)
_TRACE = re.compile(r"Traceback|File \"[^\"]+\", line \d+|\b[A-Z][a-zA-Z]+(Error|Exception):|\bat 0x[0-9a-f]+|"
                    r"\berror code \d+|\bHTTP \d{3}\b", re.I)
_STAMP = re.compile(r"\s*\(20\d\d-\d\d-\d\d\)\s*$")
_FILE = re.compile(r"(?<![<\w])(?:[~/][\w.\-]+/[\w./\-]+|[\w\-]+\.(?:pdf|docx?|xlsx?|csv|txt|png|jpe?g|key|pages|"
                   r"numbers|pptx?|zip|mov|mp4|json|py|js|md))(?![\w>])", re.I)


def lint(skill: dict) -> list[str]:
    """What makes this skill a log rather than a lesson: advisory, never blocks a write."""
    body = skill["body"]
    sections = _sections(body)
    notes = [l for l in sections.get("notes", "").splitlines() if l.strip()]
    steps = [l for l in sections.get("steps", "").splitlines() if l.strip()]
    warnings = []
    if len(body) > LINT_MAX_BODY:
        warnings.append(f"oversized: the body is {len(body)} characters (keep under {LINT_MAX_BODY}; move depth "
                        "to a references/ file)")
    if len(notes) > LINT_MAX_NOTES:
        warnings.append(f"too many notes ({len(notes)}; keep at most {LINT_MAX_NOTES} - merge them into rules)")
    for note in notes:
        text = _STAMP.sub("", note)
        if text.lstrip("-* ").startswith("Taught by demonstration"):
            continue
        if _DATED.search(text) or _TRACE.search(text):
            warnings.append(f"incident-log note (dates, times or an error dump - state the rule and why): "
                            f"{note.strip()[:120]}")
    for step in steps:
        found = _FILE.search(step)
        if found:
            warnings.append(f"a step names one file ('{found.group(0)}') - use a placeholder like <file>: "
                            f"{step.strip()[:120]}")
    return warnings


def _lint_note(skill: dict | None) -> str:
    warnings = lint(skill) if skill else []
    return (" Lint: " + "; ".join(warnings[:3]) + ".") if warnings else ""


# --- writing ----------------------------------------------------------------------------

def _creator_of(source: str) -> str:
    return {"taught": "teach", "teach": "teach", "user": "user", "auto": "auto", "seed": "seed"}.get(
        (source or "").lower(), "mint")


def _actor_for(created_by: str) -> str:
    return {"auto": "review", "teach": "teach", "seed": "seed", "user": "user"}.get(created_by, "mint")


def create(title: str, when: str, steps, notes=None, category: str = "", apps: str = "",
           source: str = "mint", actor: str = "", reason: str = "", asked: bool = False) -> tuple[dict | None, str]:
    """A new skill. `source` says who made it (created_by: auto = the review, mint = Mint in a conversation,
    user, teach, seed); `actor` who is writing (defaults from it). Same title as a saved skill = update."""
    created_by = _creator_of(source)
    actor = actor or _actor_for(created_by)
    text = " ".join([title, when, str(steps), str(notes or ""), apps])
    if has_secret(text):
        return None, ("Refused: that looks like it contains a password, key or card number. "
                      "Skills are plain files; keep secrets out of them.")
    if untrusted.refusal(text, "skill"):            # orders to the model, sending data out, hidden characters
        return None, untrusted.refusal(text, "skill")
    if not title.strip() or not steps:
        return None, "A skill needs a title and at least one step."
    category = clean_category(category) if category else _choose_category(title, when, apps)
    with _lock:
        existing = get(title, fuzzy=False)
        if existing is not None:
            return update(existing["name"], steps=steps, add_note=notes, when=when, apps=apps,
                          actor=actor, reason=reason or "saved again under the same title", asked=asked)
        path = ROOT / category / f"{slug(title)}.md"
        n = 2
        while path.exists():
            path = ROOT / category / f"{slug(title)}-{n}.md"
            n += 1
        now = time.strftime("%Y-%m-%d %H:%M")
        meta = {"title": title.strip(), "when": when.strip(), "apps": apps.strip(), "created_by": created_by,
                "source": source, "created": now, "uses": 0, "wins": 0, "fails": 0}
        _commit(path, meta, _body(steps, notes), "create", actor, reason)
    skill = _parse(path)
    return skill, f"Saved skill '{title}' in {category}." + _lint_note(skill)


def update(name: str, steps=None, add_note=None, when: str = "", title: str = "", apps: str = "",
           source: str = "", replace_notes=None, actor: str = "mint", reason: str = "", asked: bool = False,
           exact: bool = False) -> tuple[dict | None, str]:
    """Change a skill. Automatic actors (the review) changing a skill Mint did not make only add a
    Suggested note (edit_guard); nothing but the user changes a pinned one."""
    if has_secret(" ".join(str(x) for x in (steps, add_note, replace_notes, when, title, apps) if x)):
        return None, "Refused: that looks like it contains a password, key or card number."
    refused = untrusted.refusal(" ".join(str(x) for x in (steps, add_note, replace_notes, when, title, apps) if x),
                                "skill")
    if refused:
        return None, refused
    skill = get(name, fuzzy=not exact)
    if skill is None:
        return None, f"No skill called '{name}'."
    guard = edit_guard(skill, actor, asked)
    if guard == "suggest":
        ideas = [str(n) for n in ([add_note] if isinstance(add_note, str) else list(add_note or []))
                 + list(replace_notes or [])]
        return suggest(skill["name"], ideas, actor=actor, reason=reason)
    if guard:
        return None, "Refused: " + guard
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        sections = _sections(fresh["body"])
        old_steps = sections.pop("steps", "")
        old_notes = sections.pop("notes", "")
        sections.pop("_intro", None)
        notes = [l for l in old_notes.splitlines() if l.strip()]
        if replace_notes is not None:
            # A consolidated list from the review: appending forever let stale
            # workarounds pile up (one skill reached 12 notes, some contradictory).
            notes = [f"- {str(n).strip()}" for n in replace_notes if str(n).strip()][:LINT_MAX_NOTES]
            add_note = None
        for note in ([add_note] if isinstance(add_note, str) else (add_note or [])):
            note = str(note).strip()
            if note and note not in old_notes:
                notes.append(f"- {note}")
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
        body = _body(new_steps, notes, {k.title(): v for k, v in sections.items()})
        same = {k: v for k, v in meta.items() if k != "updated"} == {
            k: v for k, v in fresh["meta"].items() if k != "updated"}
        if same and body.strip() == fresh["body"].strip():
            return fresh, f"'{meta['title']}' already says that; nothing changed."
        _commit(skill["path"], meta, body, "update", actor, reason)
    fresh = _parse(skill["path"])
    return fresh, f"Updated skill '{meta['title']}'." + _lint_note(fresh)


def suggest(name: str, ideas: list[str], actor: str = "review", reason: str = "") -> tuple[dict | None, str]:
    """For a skill the user owns (user, seed, teach): ideas go under '## Suggested', the user's own steps and
    notes stay as they are. The newest MAX_SUGGESTED are kept."""
    skill = get(name, fuzzy=False)
    if skill is None:
        return None, f"No skill called '{name}'."
    if is_pinned(skill):
        return None, "Refused: " + edit_guard(skill, actor)
    ideas = [" ".join(str(i).split()) for i in ideas if str(i).strip()]
    if not ideas:
        return skill, "Nothing to suggest."
    if has_secret(" ".join(ideas)):
        return None, "Refused: that looks like it contains a password, key or card number."
    if untrusted.refusal(" ".join(ideas), "skill"):
        return None, untrusted.refusal(" ".join(ideas), "skill")
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        sections = _sections(fresh["body"])
        old = [l for l in sections.get("suggested", "").splitlines() if l.strip()]
        added = [f"- {i}" for i in ideas if i not in "\n".join(old) and i not in fresh["body"]]
        if not added:
            return fresh, f"'{fresh['title']}' already has that suggestion."
        sections["suggested"] = "\n".join((old + added)[-MAX_SUGGESTED:])
        body = _resections(sections)
        _commit(skill["path"], meta, body, "suggest", actor, reason)
    return _parse(skill["path"]), (f"'{skill['title']}' belongs to the user ({meta.get('created_by')}): added "
                                   f"{len(added)} suggested note(s) instead of changing it.")


def _resections(sections: dict[str, str]) -> str:
    """Sections back into a body, in their order (Steps, Notes first)."""
    intro = sections.pop("_intro", "")
    order = ["steps", "notes"] + [k for k in sections if k not in ("steps", "notes")]
    parts = [intro] if intro else []
    parts += [f"## {k.title()}\n{sections[k]}" for k in order if sections.get(k)]
    return "\n\n".join(parts)


def record_result(name: str, worked: bool, note: str = "", actor: str = "mint") -> str:
    skill = get(name)
    if skill is None:
        return f"No skill called '{name}'."
    with _lock:
        fresh = _parse(skill["path"]) or skill
        meta = dict(fresh["meta"])
        meta["wins" if worked else "fails"] += 1
        _write(skill["path"], meta, fresh["body"])
    if note and not is_pinned(skill):
        update(skill["name"], add_note=("Worked: " if worked else "Failed: ") + note, actor=actor, exact=True,
               reason="how following it went")
    return f"Recorded: '{skill['title']}' {'worked' if worked else 'failed'}."


def archive(name: str, actor: str = "mint", reason: str = "", skill: dict | None = None) -> str:
    """Move a skill (and its support folder) to skills/_archive - recorded, so rollback brings it back."""
    skill = skill or get(name)
    if skill is None:
        return f"No skill called '{name}'."
    if actor in AUTOMATIC and (is_pinned(skill) or skill["meta"].get("created_by") not in AUTO_EDITABLE):
        return f"Refused: '{skill['title']}' is not Mint's to remove."
    with _lock:
        target = ROOT / ARCHIVE / skill["category"] / skill["path"].name
        n = 2
        while target.exists():
            target = ROOT / ARCHIVE / skill["category"] / f"{skill['path'].stem}-{n}.md"
            n += 1
        before = _read(skill["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(skill["path"]), str(target))
        folder = support_dir(skill)
        if folder.is_dir():
            shutil.move(str(folder), str(target.with_suffix("")))
        skill_ledger.record("archive", skill["name"], _rel(skill["path"]), before, None, actor, reason,
                            title=skill["title"], to=_rel(target))
    _wrote("archive", skill["name"], actor)
    return f"Removed skill '{skill['title']}' (kept a copy in skills/{ARCHIVE}; it can be undone)."


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
    """A clear winner by shared words - about the task, not the app: app names are not counted
    (every VS Code skill shares 'code' with 'Claude Code', and 'extension' alone then decided it)."""
    app_words = {w for s in pool for a in skill_apps(s) for w in a.split()} | {
        w for a in APP_ALIASES for w in a.split()}
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower())
             if w not in _STOP and len(w) > 2 and w not in app_words}
    if not words:
        return None, ""
    scored = []
    for skill in pool:
        blob = " ".join([skill["title"], skill["meta"].get("when", ""), skill["category"]]).lower()
        have = {w for w in re.findall(r"[a-z0-9]+", blob)}
        scored.append((len(words & have) / len(words), skill))
    if not scored:
        return None, ""
    scored.sort(key=lambda x: -x[0])
    top = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if top[0] >= 0.5 and top[0] - runner >= 0.2:
        return top[1], f"word match ({top[0]:.2f}; Jev was unreachable)"
    return None, ""


# --- direct edits (the Skills & Memory window) ----------------------------------------------

def save_raw(path: Path, title: str, when: str, apps: str, category: str, body: str) -> tuple[Path, str]:
    """Save a skill exactly as edited by hand. Moves the file (and its support folder) if the category
    or title changed. Returns (new path, message)."""
    text = " ".join([title, when, apps, body])
    if has_secret(text):
        return path, "Refused: that looks like it contains a password, key or card number."
    if untrusted.refusal(text, "skill"):
        return path, untrusted.refusal(text, "skill")
    if not title.strip():
        return path, "A skill needs a title."
    with _lock:
        fresh = _parse(path) or {"meta": {"uses": 0, "wins": 0, "fails": 0, "created_by": "user"}, "body": ""}
        meta = dict(fresh["meta"])
        meta.update(title=title.strip(), when=" ".join(when.split()), apps=apps.strip(),
                    updated=time.strftime("%Y-%m-%d %H:%M"))
        if "edited" not in meta.get("source", ""):
            meta["source"] = f"{meta.get('source', '')}+edited".strip("+")
        target = ROOT / clean_category(category) / f"{slug(title)}.md"
        if target != path and target.exists():
            return path, f"A skill called '{title}' already exists in {clean_category(category)}."
        if target == path:
            _commit(path, meta, body, "update", "user", "edited in Skills & Memory")
            return target, "Saved."
        before = _read(path)
        _write(target, meta, body)
        if path.exists():
            path.unlink()
        if path.with_suffix("").is_dir() and not target.with_suffix("").exists():
            shutil.move(str(path.with_suffix("")), str(target.with_suffix("")))
        skill_ledger.record("move", target.stem, _rel(path), before, _read(target), "user",
                            "renamed or moved in Skills & Memory", title=meta["title"], to=_rel(target))
    _wrote("move", target.stem, "user")
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
            "created_by": "user", "source": "user", "created": time.strftime("%Y-%m-%d %H:%M"), "uses": 0,
            "wins": 0, "fails": 0}
    with _lock:
        _commit(path, meta, _body(["First step (name the tool and the exact on-screen label)"], ["Gotchas go here"]),
                "create", "user", "New in Skills & Memory")
    return path


def set_pinned(path: Path, pinned: bool) -> str:
    """Pin (nothing automatic may change or remove it) or unpin. The user's own action."""
    with _lock:
        fresh = _parse(path)
        if fresh is None:
            return "That skill is gone."
        meta = dict(fresh["meta"])
        if pinned:
            meta["pinned"] = "yes"
        else:
            meta.pop("pinned", None)
        _commit(path, meta, fresh["body"], "pin" if pinned else "unpin", "user")
    return (f"Pinned '{fresh['title']}': Mint will not change it by itself." if pinned
            else f"Unpinned '{fresh['title']}'.")


# --- history and undo (the ledger) ---------------------------------------------------------

_STATS = ("uses", "wins", "fails", "last_used", "state")
LEARNERS = ("review", "mint", "learn", "curator", "job")     # changes Mint made, not the user


def history(name: str, limit: int = 10) -> list[dict]:
    """A skill's changes, newest first (by file name, or a title that matches one)."""
    wanted = slug(name)
    rows = skill_ledger.entries(wanted, limit)
    if rows:
        return rows
    skill = get(name, fuzzy=False)
    return skill_ledger.entries(skill["name"], limit) if skill else []


def _inside(rel: str) -> Path | None:
    """skills/<rel>, only when it really is inside skills/ (a hand-edited ledger must not write elsewhere)."""
    if not rel:
        return None
    root = ROOT.resolve()
    target = (ROOT / rel).resolve()
    return ROOT / rel if root in target.parents else None


def _with_stats(text: str, current: str | None) -> str:
    """The restored text, keeping the counters the file has now (undoing a change of words does not undo
    the times the skill was used)."""
    if not current:
        return text
    old = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    now = re.match(r"^---\n(.*?)\n---\n?", current, re.S)
    if not old or not now:
        return text
    meta = dict(line.split(":", 1) for line in old.group(1).splitlines() if ":" in line)
    have = dict(line.split(":", 1) for line in now.group(1).splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()}
    for key in _STATS:
        if key in {k.strip() for k in have}:
            meta[key] = next(v.strip() for k, v in have.items() if k.strip() == key)
        else:
            meta.pop(key, None)
    return _render(meta, old.group(2))


def rollback(entry_id: str, actor: str = "user") -> tuple[bool, str]:
    """Put back what a ledger entry changed. The undo is itself an entry, so it can be undone too."""
    entry = skill_ledger.get(entry_id)
    if entry is None:
        return False, f"No change '{entry_id}' in the skill history."
    src, dst = _inside(entry.get("path", "")), _inside(entry.get("to", "")) if entry.get("to") else None
    if src is None or (entry.get("to") and dst is None):
        return False, "Refused: that history entry points outside the skills folder."
    before = skill_ledger.read_blob(entry.get("before")) if entry.get("before") else None
    if entry.get("before") and before is None:
        return False, "The saved text for that change is missing; nothing was changed."
    title = entry.get("title") or entry.get("skill", "")
    with _lock:
        if dst is not None:
            # archive / move: back to where it was
            current = _read(dst)
            if dst.exists():
                if before is not None and current != before:
                    dst.unlink()
                else:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
            if before is not None:
                src.parent.mkdir(parents=True, exist_ok=True)
                src.write_text(_with_stats(before, current))
            folder = dst.with_suffix("")
            if folder.is_dir() and not src.with_suffix("").exists():
                shutil.move(str(folder), str(src.with_suffix("")))
            skill_ledger.record("rollback", entry.get("skill", ""), _rel(dst), current, None, actor,
                                f"undo of {entry.get('action')} [{entry_id}]", title=title, to=_rel(src),
                                undoes=entry_id)
            what = "brought back"
        elif before is None:
            # it was created: undoing it archives it (restorable), never deletes
            current = _read(src)
            if current is None:
                return False, f"'{title}' is already gone."
            if not entry.get("file"):
                skill = _parse(src)
                target = ROOT / ARCHIVE / (skill["category"] if skill else "general") / src.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(target))
                if src.with_suffix("").is_dir():
                    shutil.move(str(src.with_suffix("")), str(target.with_suffix("")))
                skill_ledger.record("rollback", entry.get("skill", ""), _rel(src), current, None, actor,
                                    f"undo of create [{entry_id}]", title=title, to=_rel(target), undoes=entry_id)
            else:
                src.unlink()
                skill_ledger.record("rollback", entry.get("skill", ""), _rel(src), current, None, actor,
                                    f"undo of create [{entry_id}]", title=title, undoes=entry_id)
            what = "removed (kept in the archive)" if not entry.get("file") else "removed"
        else:
            current = _read(src)
            restored = before if entry.get("file") else _with_stats(before, current)
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(restored)
            skill_ledger.record("rollback", entry.get("skill", ""), _rel(src), current, restored, actor,
                                f"undo of {entry.get('action')} [{entry_id}]", title=title, undoes=entry_id)
            what = "restored to before that change"
    _wrote("rollback", entry.get("skill", ""), actor)
    return True, f"Undid {entry.get('action')} of '{title}' by {entry.get('actor')}: {what}."


def undo_last(name: str = "", actor: str = "user", learned_only: bool = False) -> str:
    """Undo the newest change to a skill (or, with no name, the newest change Mint made by itself)."""
    if name:
        rows = history(name, 30)
        if not rows:
            return f"No recorded changes to a skill called '{name}'."
    else:
        rows = skill_ledger.entries(limit=200)
        learned_only = True
    if learned_only:
        rows = [r for r in rows if r.get("actor") in LEARNERS and r.get("action") != "rollback"]
    if not rows:
        return ("Nothing Mint learned by itself is recorded for that skill." if name
                else "Nothing Mint learned by itself is recorded yet.")
    _ok, message = rollback(rows[0]["id"], actor)
    return message


# --- the curator: keeps the library from silting up (no LLM) ----------------------------------

def _when(text: str) -> float | None:
    try:
        return time.mktime(time.strptime(str(text).strip()[:16], "%Y-%m-%d %H:%M"))
    except (ValueError, OverflowError):
        return None


def curate(now: float | None = None, force: bool = False) -> dict:
    """At most once every CURATE_EVERY: skills Mint made that went unused for STALE_DAYS turn stale
    (listed last, still found); stale ones the review made, unused for ARCHIVE_DAYS, are archived
    (recorded, restorable). Pinned skills and the user's, seeds and lessons are never touched.
    The first run only starts the clock."""
    import json
    now = time.time() if now is None else now
    counts = {"checked": 0, "stale": 0, "archived": 0, "reactivated": 0}
    state_path = ROOT / ".curator.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    last = float(state.get("last_run") or 0)
    if not force:
        if not last:
            ROOT.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"last_run": now, "note": "clock started"}))
            return counts
        if now - last < CURATE_EVERY:
            return counts
    for skill in all_skills():
        meta = skill["meta"]
        if is_pinned(skill) or meta.get("created_by") not in AUTO_EDITABLE:
            continue
        counts["checked"] += 1
        anchor = _when(meta.get("last_used", "")) or _when(meta.get("created", "")) or _when(meta.get("updated", ""))
        if anchor is None:
            continue
        idle_days = (now - anchor) / 86400
        if idle_days >= ARCHIVE_DAYS and is_stale(skill) and meta.get("created_by") == "auto":
            if archive(skill["name"], actor="curator", skill=skill,
                       reason=f"stale and unused for {int(idle_days)} days").startswith("Removed"):
                counts["archived"] += 1
        elif idle_days >= STALE_DAYS and not is_stale(skill):
            with _lock:
                _write(skill["path"], dict(meta, state="stale"), skill["body"])
            counts["stale"] += 1
        elif idle_days < STALE_DAYS and is_stale(skill):
            with _lock:
                fresh = dict(meta)
                fresh.pop("state", None)
                _write(skill["path"], fresh, skill["body"])
            counts["reactivated"] += 1
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_run": now, "counts": counts}))
    if counts["stale"] or counts["archived"] or counts["reactivated"]:
        log.info("skill curator: %s", counts)
    return counts
