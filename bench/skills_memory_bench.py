"""Offline regression bench for the parts Jev decides: skills, memory, app names.

Nothing here touches the screen. Skills and memory run in a temporary folder
with the shipped seed skills, against the real Jev (TypeSafe) service.

    .venv/bin/python bench/skills_memory_bench.py            # everything
    .venv/bin/python bench/skills_memory_bench.py skills     # one section

Prints each case, its latency, and a pass rate per section.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mint import appfinder, membank, skillbook  # noqa: E402

SKILL_CASES = [
    ("open chatgpt and in the data labeling portal project ask it to improve the UI", "start-a-chat-in-a-project"),
    ("make a new chatgpt project called research", "create-a-project"),
    ("find my old chat about the resume parser in chatgpt", "search-chats"),
    ("open z code and build a sword story game with GLM", "start-a-new-task"),
    ("continue my last claude code session in cf-backend", "start-or-resume-a-session"),
    ("use codex to add tests to the parser", "run-codex"),
    ("go to the on call channel in slack", "open-a-channel"),
    ("write my standup notes into a google doc and export a pdf", "write-a-new-doc"),
    ("the click didn't do anything, try again", "when-a-click-does-nothing"),
    ("set a timer for ten minutes", None),
    ("what's the weather like", None),
    ("turn the volume down", None),
    ("search the web: what is the latest stable version of python", None),
    ("read the notes file on my desktop and summarise it", None),
    ("in finder use the menu to show the path bar", None),
    ("ask chatgpt to research honeybees and read me its answer", "research-a-topic-and-read-output-in-chatgpt"),
]

FACTS = [
    "I'm Alex, a developer at Acme",
    "My manager is Meera",
    "Standup is at 9pm IST every weekday",
    "My work Chrome profile is alex@example.com",
    "The on-call Slack channel is oncall-support",
    "My sister's birthday is 12 December",
    "I like lo-fi music while coding",
    "My home city is Gurgaon",
    "I use ZCode with the GLM model for side projects",
    "The data-labeling-portal is a ChatGPT project I work in",
]

RECALL_CASES = [
    ("who is my boss?", {"My manager is Meera"}),
    ("when is standup and where do incidents go", {"Standup is at 9pm IST every weekday",
                                                   "The on-call Slack channel is oncall-support"}),
    ("open my work chrome", {"My work Chrome profile is alex@example.com"}),
    ("what should I get my sister and when", {"My sister's birthday is 12 December"}),
    ("what's 2 + 2", set()),
    ("which coding app do I use for side projects", {"I use ZCode with the GLM model for side projects"}),
]

UPDATES = [
    ("My manager is now Rahul", "My manager is Meera"),
    ("Standup moved to 8:30pm IST", "Standup is at 9pm IST every weekday"),
    ("I also like jazz", None),                   # a new fact, replaces nothing
]

APPS = [("Xcode", "ZCode"), ("slak", "Slack"), ("chat gpt", "ChatGPT"), ("vs code", "Visual Studio Code"),
        ("visual studio", "Visual Studio Code"), ("Photoshop", None), ("terminal", "Terminal")]


def _timed(fn, *args):
    started = time.monotonic()
    result = fn(*args)
    return result, time.monotonic() - started


def bench_skills() -> float:
    ok = 0
    for task, want in SKILL_CASES:
        (skill, why), took = _timed(skillbook.find, task)
        got = skill["name"] if skill else None
        hit = got == want
        ok += hit
        print(f"  {'PASS' if hit else 'FAIL'} {took:4.1f}s  {task[:52]!r:56} -> {got} (want {want})")
    return ok / len(SKILL_CASES)


def bench_memory() -> float:
    for fact in FACTS:
        membank.add(fact)
    ok = total = 0
    for question, want in RECALL_CASES:
        found, took = _timed(membank.relevant, question)
        got = {b["text"] for b in found}
        precise = got == want
        total += 1
        ok += precise
        print(f"  {'PASS' if precise else 'FAIL'} {took:4.1f}s  recall {question!r:48} -> {sorted(got)}")
    for new, replaces in UPDATES:
        before = {b["text"] for b in membank.blocks()}
        message, took = _timed(membank.add, new)
        after = {b["text"] for b in membank.blocks()}
        hit = (replaces in before and replaces not in after) if replaces else (before <= after)
        total += 1
        ok += hit
        print(f"  {'PASS' if hit else 'FAIL'} {took:4.1f}s  add {new!r:40} -> {message[:70]}")
    return ok / total


def bench_apps() -> float:
    ok = 0
    for said, want in APPS:
        (got, how), took = _timed(appfinder.resolve, said)
        hit = got == want
        ok += hit
        print(f"  {'PASS' if hit else 'FAIL'} {took:4.1f}s  {said!r:18} -> {got} ({how[:50]})")
    return ok / len(APPS)


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mint-bench-"))
    skillbook.ROOT = tmp / "skills"
    runtime = pathlib.Path.home() / "Library/Application Support/Mint/skills/apps/chatgpt"
    extra = runtime / "research-a-topic-and-read-output-in-chatgpt.md"
    if extra.exists():                      # made by Mint at runtime, not shipped as a seed
        (skillbook.ROOT / "apps/chatgpt").mkdir(parents=True, exist_ok=True)
        (skillbook.ROOT / "apps/chatgpt" / extra.name).write_text(extra.read_text())
    membank.ROOT = tmp / "memory"
    membank.BANK = membank.ROOT / "bank.json"
    membank.VIEW = membank.ROOT / "MEMORY.md"
    membank.LEGACY = tmp / "none.md"
    wanted = set(sys.argv[1:]) or {"skills", "memory", "apps"}
    scores = {}
    for name, fn in (("skills", bench_skills), ("memory", bench_memory), ("apps", bench_apps)):
        if name in wanted:
            print(f"\n{name}")
            scores[name] = fn()
    print("\n" + "  ".join(f"{k}: {v:.0%}" for k, v in scores.items()))
    return 0 if all(v >= 0.8 for v in scores.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
