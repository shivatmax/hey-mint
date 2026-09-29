"""Mint reliability benchmark: 70 realistic long-horizon tasks, each set up, run and checked.

    PY="$HOME/Library/Application Support/Mint/.venv/bin/python"
    "$PY" bench/reliability/run.py --list
    "$PY" bench/reliability/run.py --dry-run                 # fixtures only, no Mint
    "$PY" bench/reliability/run.py --only web-local-fact,clip-to-file
    "$PY" bench/reliability/run.py --category web --repeat 3
    "$PY" bench/reliability/run.py                           # all 70 (about 2-3 hours)

See README.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rb import apple as ap  # noqa: E402
from rb import mintlink, sandbox as sb  # noqa: E402
from rb import t_docs, t_files, t_hard, t_hard2, t_misc, t_pim, t_sheets, t_web  # noqa: E402,F401 - they register
from rb.oracles import ORACLES  # noqa: E402
from rb.registry import CHECKS, CLEANUPS, SETUPS, Ctx, Skip, Verdict  # noqa: E402
from rb.site import SiteServer  # noqa: E402

TASKS = HERE / "tasks.json"
RESULTS = HERE / "results"
APPLE_NEEDS = ("reminders", "calendar", "notes", "mail")


class _Fill(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def load_tasks() -> list[dict]:
    tasks = json.loads(TASKS.read_text())["tasks"]
    for task in tasks:
        for name in [task["setup"], task["check"], *task.get("cleanup", []),
                     *[t["check"] for t in task["turns"] if isinstance(t, dict) and t.get("check")]]:
            if name not in SETUPS and name not in CHECKS and name not in CLEANUPS:
                raise SystemExit(f"{task['id']}: no function named {name!r}")
    return tasks


def display_root() -> str:
    home = str(sb.HOME)
    return "~" + str(sb.ROOT)[len(home):] if str(sb.ROOT).startswith(home) else str(sb.ROOT)


# --- one task -----------------------------------------------------------------------------------------------

def run_check(ctx: Ctx, name: str, settle: float, poll: bool) -> Verdict:
    """Checks are read-only, so they are polled until they pass or `settle` runs out (background
    work such as video edits and sub-agents finishes after Mint's turn)."""
    deadline = time.monotonic() + (settle if poll else 0)
    while True:
        try:
            verdict = CHECKS[name](ctx)
        except (ap.Unavailable, Skip):
            raise
        except Exception as error:  # noqa: BLE001 - a crashing check is a failed check
            verdict = Verdict(False, f"check crashed: {error!r}"[:300])
        if verdict.ok or time.monotonic() >= deadline:
            return verdict
        time.sleep(3)


def standard_cleanup(ctx: Ctx, user_clipboard: str) -> dict:
    """Leave nothing behind: the sandbox, the Apple containers, strays, pins, memories, apps."""
    task = ctx.task
    report: dict = {}
    since = dt.datetime.fromtimestamp(ctx.started) - dt.timedelta(minutes=1)
    markers = ("MintBench", *task.get("markers", []))
    steps = {"reminders": lambda: ap.reminders_cleanup(ctx.apple, markers, since),
             "calendar": lambda: ap.calendar_cleanup(ctx.apple, markers, since),
             "notes": lambda: ap.notes_cleanup(ctx.apple, markers, since),
             "mail": lambda: (ap.mail_cleanup(ctx.apple), time.sleep(8), ap.mail_cleanup(ctx.apple))}
    for need in task.get("needs", []):
        if need in steps and need in ctx.apple.chosen:
            try:
                steps[need]()
                if need != "mail" and ap.container_exists(ctx.apple, need):
                    report.setdefault("leftovers", []).append(f"the Mint Bench {need} container is still there")
            except (ap.Unavailable, RuntimeError) as error:
                report.setdefault("cleanup_errors", []).append(f"{need}: {error}"[:300])
    if ap.permission("finder") == 0:            # cosmetic: only when it needs no prompt
        try:
            ap.finder_close_sandbox(ap.Apple("direct"))
        except (ap.Unavailable, RuntimeError):
            pass
    started_apps = (sb.launched_apps() - ctx.apps_before) & sb.VIEWERS
    if started_apps:
        report["apps_quit"] = sb.quit_apps(started_apps)
    strays = sb.find_strays(ctx.started, tuple(m for m in task.get("markers", []) if len(m) >= 5))
    if strays:
        report["strays_removed"] = sb.remove_strays(strays)
    if ctx.trashed:
        purged = sb.purge_trash(ctx.trashed)
        if purged:
            report["trash_purged"] = purged
    pins = sb.pins_remove(("mintbench",))
    if pins:
        report["pins_removed"] = pins
    memories = sb.bank_remove(("mintbench", "mint bench"))
    if memories or ctx.data.get("memories_removed"):
        report["memories_removed"] = list(ctx.data.get("memories_removed", [])) + memories
    if user_clipboard is not None and sb.clipboard_get() != user_clipboard and sb.clipboard_is_bench():
        sb.clipboard_set(user_clipboard)
    sb.wipe_root()
    if sb.ROOT.exists():
        report.setdefault("leftovers", []).append(f"{sb.ROOT} could not be removed")
    return report


def run_task(task: dict, rep: int, args, site: SiteServer, apple: ap.Apple, user_clipboard: str) -> dict:
    result = {"id": task["id"], "category": task["category"], "difficulty": task["difficulty"],
              "complications": task.get("complications", []), "repeat": rep, "status": "error", "passed": False,
              "reason": "", "seconds": 0.0, "turns": [], "tool_calls": 0, "errors": [], "mid_checks": [],
              "log_excerpt": "", "cleanup": {}}
    started = time.monotonic()
    ctx = Ctx(task=task, site=site.url, apple=apple, server=site, dry=args.dry_run)
    sb.reset_root()
    ctx.apps_before = sb.launched_apps()
    log_start = mintlink.log_size()
    try:
        for need in task.get("needs", []):
            if need in APPLE_NEEDS:
                apple.route(need)
            elif need == "internet" and args.offline:
                raise Skip("needs the internet (--offline)")
        ctx.started = time.time()
        SETUPS[task["setup"]](ctx)
        fill = _Fill(root=display_root(), site=site.url, **{k: v for k, v in ctx.data.items() if isinstance(v, str)})
        for spec in task["turns"]:
            spec = spec if isinstance(spec, dict) else {"say": spec}
            say = spec["say"].format_map(fill)
            if args.dry_run:
                result["turns"].append({"say": say, "status": "dry-run"})
            else:
                turn = mintlink.run_turn(say, timeout=task.get("timeout", 240), wait=spec.get("wait", True),
                                         quiet=args.quiet)
                if not spec.get("wait", True):
                    time.sleep(spec.get("delay", 3))
                result["turns"].append(turn.as_dict())
                print(f"    turn {len(result['turns'])}: {turn.status} in {turn.seconds:.0f}s, "
                      f"{len(turn.tools)} tool calls" + (f", {len(turn.errors)} errors" if turn.errors else ""))
            if spec.get("check") and not args.selftest:
                mid = run_check(ctx, spec["check"], 20, poll=not args.dry_run)
                result["mid_checks"].append({"check": spec["check"], "ok": mid.ok, "reason": mid.reason})
        if args.selftest:
            if task["id"] not in ORACLES:
                raise Skip("no oracle for this task")
            ORACLES[task["id"]](ctx)
        verdict = run_check(ctx, task["check"], task.get("settle", 10), poll=not args.dry_run)
        mids_ok = all(m["ok"] for m in result["mid_checks"])
        result["passed"] = verdict.ok and mids_ok
        result["reason"] = verdict.reason if mids_ok else next(m["reason"] for m in result["mid_checks"] if not m["ok"])
        result["status"] = "pass" if result["passed"] else "fail"
        if not args.dry_run and any(t.get("status") == "not-received" for t in result["turns"]):
            result["status"], result["reason"] = "fail", "Mint did not receive the request (not running?)"
    except (Skip, ap.Unavailable) as why:
        result["status"], result["reason"] = "skip", str(why)
    except Exception as error:  # noqa: BLE001 - one broken task must not stop the run
        result["status"], result["reason"] = "error", f"{type(error).__name__}: {error}"[:400]
        result["traceback"] = traceback.format_exc()[-2000:]
    finally:
        for name in task.get("cleanup", []):
            try:
                CLEANUPS[name](ctx)
            except Exception as error:  # noqa: BLE001
                result["cleanup"].setdefault("cleanup_errors", []).append(f"{name}: {error!r}"[:300])
        if not args.keep:
            result["cleanup"].update(standard_cleanup(ctx, user_clipboard))
        if not args.dry_run and result["turns"]:
            mintlink.stop_and_settle()
    result["seconds"] = round(time.monotonic() - started, 1)
    if not args.dry_run and result["turns"]:
        # The whole task's log, including turns sent without waiting (an interruption).
        text = mintlink.read_from(log_start)
        whole = mintlink.Turn(say="")
        mintlink.parse(text, whole, "")
        result["tool_calls"] = len(whole.tools)
        result["errors"] = whole.errors
        result["log_excerpt"] = "\n".join(line for line in text.splitlines() if line.strip())[-12000:]
    return result


# --- report --------------------------------------------------------------------------------------------------

def _rate(rows: list[dict]) -> str:
    counted = [r for r in rows if r["status"] != "skip"]
    if not counted:
        return "-"
    passed = sum(r["passed"] for r in counted)
    return f"{passed}/{len(counted)} ({100 * passed / len(counted):.0f}%)"


def _reason_key(result: dict) -> str:
    if result["status"] == "error":
        return "harness error: " + result["reason"].split(":")[0]
    turns = [t.get("status") for t in result["turns"]]
    reason = result["reason"]
    if "timeout" in turns and not result["passed"]:
        reason = "turn timed out; " + reason
    return re.sub(r"\d+", "N", reason.split(" (")[0])[:90]


def markdown(meta: dict, results: list[dict]) -> str:
    out = [f"# Mint reliability run {meta['stamp']}", "",
           f"- Mode: {meta['mode']}; tasks: {meta['tasks']}, repeat {meta['repeat']}",
           f"- Pass rate (skips excluded): **{_rate(results)}**; skipped: {sum(r['status'] == 'skip' for r in results)}; "
           f"harness errors: {sum(r['status'] == 'error' for r in results)}",
           f"- Time: {meta['seconds'] / 60:.1f} min; site {meta['site']}; fixtures: {meta['fixtures']}", ""]
    for title, key in (("category", lambda r: [r["category"]]), ("difficulty", lambda r: [r["difficulty"]]),
                       ("complication", lambda r: r["complications"] or ["none"])):
        groups: dict[str, list] = defaultdict(list)
        for r in results:
            for k in key(r):
                groups[k].append(r)
        out += [f"## By {title}", "", f"| {title} | pass rate | skipped |", "|---|---|---|"]
        out += [f"| {k} | {_rate(v)} | {sum(x['status'] == 'skip' for x in v)} |" for k, v in sorted(groups.items())]
        out.append("")
    failures = Counter(_reason_key(r) for r in results if r["status"] in ("fail", "error"))
    out += ["## Top failure reasons", ""]
    out += [f"{n}. {reason} - {count}x" for n, (reason, count) in enumerate(failures.most_common(12), 1)] or ["None."]
    out += ["", "## Tasks", "", "| task | result | secs | tools | errors | reason |", "|---|---|---|---|---|---|"]
    for r in results:
        rid = r["id"] + (f" #{r['repeat']}" if meta["repeat"] > 1 else "")
        reason = r["reason"].replace("|", "/")[:140]
        out.append(f"| {rid} | {r['status'].upper()} | {r['seconds']:.0f} | {r['tool_calls']} | {len(r['errors'])} | {reason} |")
    tools = Counter(t["name"] for r in results for turn in r["turns"] for t in turn.get("tools", []))
    if tools:
        out += ["", "## Tools used", "", ", ".join(f"{n} {c}" for n, c in tools.most_common())]
    notes = []
    for r in results:
        c = r["cleanup"]
        for key in ("strays_removed", "leftovers", "cleanup_errors", "memories_removed", "trash_purged", "apps_quit"):
            if c.get(key):
                notes.append(f"- {r['id']}: {key.replace('_', ' ')}: {c[key]}")
        others = [o for t in r["turns"] for o in t.get("other_requests", [])]
        if others:
            notes.append(f"- {r['id']}: other requests reached Mint during the task (another tester?): {others[:3]}")
    if meta.get("new_skills"):
        notes.append(f"- Skills Mint saved during the run (not removed): {meta['new_skills']}")
    out += ["", "## Cleanup and side effects", ""] + (notes or ["Nothing left behind."])
    return "\n".join(out) + "\n"


# --- main ------------------------------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated task ids (prefixes work: 'web-' runs all web tasks)")
    parser.add_argument("--category", help="comma-separated categories")
    parser.add_argument("--difficulty", help="easy, medium or hard (comma-separated)")
    parser.add_argument("--repeat", type=int, default=1, help="run each task N times")
    parser.add_argument("--dry-run", action="store_true", help="setup, check and cleanup only; Mint is not used")
    parser.add_argument("--selftest", action="store_true",
                        help="like --dry-run, but an oracle makes the correct end state: every check must PASS")
    parser.add_argument("--fixtures", choices=("auto", "eventkit", "direct", "mint", "off"), default="auto",
                        help="how Reminders/Calendar/Notes/Mail fixtures are scripted (see rb/apple.py)")
    parser.add_argument("--no-prompt", action="store_true",
                        help="skip Apple-app tasks rather than risk a macOS permission prompt")
    parser.add_argument("--offline", action="store_true", help="skip tasks that need the internet")
    parser.add_argument("--keep", action="store_true", help="no cleanup (debugging one task)")
    parser.add_argument("--quiet", type=float, default=mintlink.QUIET, help="seconds of quiet that end a turn")
    parser.add_argument("--port", type=int, default=8765, help="local test site port (0 = any)")
    parser.add_argument("--list", action="store_true", help="list the tasks and exit")
    args = parser.parse_args()
    args.dry_run = args.dry_run or args.selftest

    tasks = load_tasks()
    if args.only:
        wanted = [w.strip() for w in args.only.split(",") if w.strip()]
        tasks = [t for t in tasks if any(t["id"] == w or (w.endswith("-") and t["id"].startswith(w)) for w in wanted)]
    if args.category:
        tasks = [t for t in tasks if t["category"] in args.category.split(",")]
    if args.difficulty:
        tasks = [t for t in tasks if t["difficulty"] in args.difficulty.split(",")]
    if args.list:
        for t in tasks:
            print(f"{t['id']:28} {t['category']:26} {t['difficulty']:7} {len(t['turns'])} turn(s)  "
                  f"{', '.join(t.get('complications', []))}")
        print(f"{len(tasks)} tasks")
        return 0
    if not tasks:
        print("No tasks match.")
        return 1
    if not args.dry_run and not mintlink.mint_running():
        print("Mint is not loaded right now; the first request starts it (Mint Ear passes it on).")
    if sb.ROOT.exists() and any(sb.ROOT.iterdir()):
        print(f"{sb.ROOT} exists and is not empty; it is the benchmark's sandbox and will be wiped.")

    site = SiteServer(args.port).start()
    apple = ap.Apple(args.fixtures, allow_prompts=not args.no_prompt)
    user_clipboard = sb.clipboard_get()
    skills_before = sb.skills_snapshot()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(exist_ok=True)
    began = time.monotonic()
    results = []
    print(f"MintBench: {len(tasks)} task(s) x {args.repeat}{' (dry run)' if args.dry_run else ''}; site {site.url}")
    try:
        for rep in range(1, args.repeat + 1):
            for task in tasks:
                print(f"[{len(results) + 1}/{len(tasks) * args.repeat}] {task['id']}")
                result = run_task(task, rep, args, site, apple, user_clipboard)
                results.append(result)
                print(f"    -> {result['status'].upper()} {result['reason'][:150]} ({result['seconds']:.0f}s)")
                if result["cleanup"].get("leftovers"):
                    print(f"    !! leftovers: {result['cleanup']['leftovers']}")
    except KeyboardInterrupt:
        print("Interrupted; cleaning up.")
    finally:
        if "mail" in apple.chosen:
            # Mail autosaves a compose message into Drafts a few seconds after it is made, even after
            # it was closed: one last sweep once that has happened.
            time.sleep(10)
            try:
                ap.mail_cleanup(apple)
            except (ap.Unavailable, RuntimeError) as error:
                print(f"Mail cleanup failed: {error}")
        sb.wipe_root()
        if sb.clipboard_get() != user_clipboard and sb.clipboard_is_bench():
            sb.clipboard_set(user_clipboard)
        site.stop()
    mode = "self-test (oracles, no Mint)" if args.selftest else "dry run (no Mint)" if args.dry_run else "live"
    meta = {"stamp": stamp, "dry_run": args.dry_run, "mode": mode, "tasks": len(tasks), "repeat": args.repeat,
            "seconds": time.monotonic() - began, "site": site.url, "fixtures": dict(apple.chosen) or args.fixtures,
            "new_skills": sorted(sb.skills_snapshot() - skills_before), "argv": sys.argv[1:]}
    suffix = "-selftest" if args.selftest else "-dry" if args.dry_run else ""
    (RESULTS / f"{stamp}{suffix}.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1,
                                                               ensure_ascii=False, default=str))
    report = markdown(meta, results)
    (RESULTS / f"{stamp}{suffix}.md").write_text(report)
    print(f"\nPass rate {_rate(results)}.  Results: {RESULTS / (stamp + suffix)}.json / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
