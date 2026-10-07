#!/usr/bin/env python3
"""How often are Mint's coding-agent checks right? Scores what mint/agent_tests.py says about real Claude Code requests
against a person's hand-labeled answers (cases/*.json, from dotpals: see cases/README.md).

    python tests/accuracy/score.py            the table, and every claim it gets wrong or misses
    python tests/accuracy/score.py --json     the same as JSON
    python tests/accuracy/score.py --save     record today's score as the baseline (baseline.json) that
                                              tests/test_accuracy.py holds it to

Each case is one finished request: its steps (commands run with their output, files edited with their patch) and the
true answer for each claim. The steps are fed to Mint's own functions the way agent_watch.py feeds a live session:

  tests     every finished command dotpals treats as a test run (is_test), read with verdict(cmd, output, exit code):
            passed / failed / unclear. A labeled run Mint doesn't see as a test, or calls unclear though a person can
            tell, is "unsure" (a missing answer, not a false one). One Mint calls a test that isn't labeled is wrong.
  why       each failed run a person gave a reason for (or none): the verdict's reason. Right when the two agree (see
            _why_matches): the truth's first meaningful fragment is in Mint's reason, or Mint's reason is in the truth.
            No reason where the output shows one: unsure.
  weakened  agent_watch's rule: an edit to a test file while the last test run failed, read with weakened(diff lines,
            path). Right when both say nothing, or both say the same kinds of weakening (removed / skip / changed
            expectation). The edit's diff is its body.patch ("-old" / "+new" lines).
  risky     risk() on every command that ran (with an exit code: a denied one never ran) and every file it changed,
            mapped to dotpals' warning texts with RISK_MAP. Each warning counts once per request, like dotpals' lists:
            one said and not true, or true and not said, is wrong.
  retries   a failed run (a failed verdict, or a non-zero exit) and the next runs of the same command (same_command)
            within 25 steps and 15 minutes: "fixed" when one stops failing, "failing" when none does. A failed test run
            is also fixed by a later passing run of the whole suite (whole_suite). Only real retries (two tries or
            more) are claims.

Not scored: ready to merge, why it stopped and what changed (agent_tests makes no such claims), and "the same command
failed 3 times" (agent_watch's counter, not agent_tests').
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
BASELINE = HERE / "baseline.json"

for _p in (os.getcwd(), str(HERE.parents[1]), str(HERE.parents[3] / "jarvis")):   # jarvis/, or the public repo's root
    if os.path.isdir(os.path.join(_p, "mint")) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from mint.tools import agent_tests as at
except ImportError:
    from mint import agent_tests as at

KINDS = ("tests", "why", "weakened", "risky", "retries")
LABEL = {"tests": "Test results", "why": "Why tests failed", "weakened": "Tests changed", "risky": "Risky steps",
         "retries": "Retries"}

# Mint's risk() flag -> dotpals' warning text. Only the warnings both make; Mint's others ("read .env", which dotpals
# words as a note, not a warning) aren't compared. dotpals' "The same command failed 3 times" is agent_watch's in Mint.
RISK_MAP = (
    (r"rm -rf", "Deleted files with a recursive delete"),
    (r"force-push", "Force-pushed to git"),
    (r"git (?:reset --hard|clean -f|checkout --|restore \.)", "Threw away changes in git"),
    (r"(?:DROP (?:TABLE|DATABASE)|TRUNCATE TABLE)", "Dropped database tables"),
    (r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod) \| \S+", "Ran a script straight from the internet"),
    (r"sudo|chmod 777", "Changed system permissions"),
    (r"taskkill|stop-process|kill -(?:9|KILL|SIGKILL)|pkill|killall", "Force-stopped programs"),
    (r"changed (.+)", "Changed {0}, which usually holds secrets"),
)
_DOTPALS_WARNINGS = re.compile(r"Deleted files with|Force-pushed|Threw away|Dropped database|Ran a script straight"
                               r"|Changed system permissions|Force-stopped|Changed .+, which usually holds secrets")


def mapped_risk(flag: str) -> str | None:
    for pattern, text in RISK_MAP:
        m = re.fullmatch(pattern, flag, re.I)
        if m:
            return text.format(*m.groups())
    return None


# -- a case's steps, as Mint reads them -------------------------------------------------------------------------------

def _command(e) -> str:
    return str((e.get("body") or {}).get("command") or "")


def _output(e) -> str:
    return f"{(e.get('body') or {}).get('output') or ''}\n{e.get('error') or ''}"


def _exit(e):
    """(ran, exit code, interrupted), as agent_watch reads a Claude Bash result: a failed one says "Exit code N"; one that
    failed without it never ran (denied, cut short)."""
    if e.get("status") == "stopped":
        return True, None, True
    if e.get("exitUnknown"):
        return True, None, False
    if e.get("status") == "failed":
        m = re.match(r"(?:Error: )?Exit code (-?\d+)", str(e.get("error") or ""))
        return (True, int(m[1]), False) if m else (False, None, False)
    return True, 0, False


def _finished(e) -> bool:
    return e.get("status") not in ("running", "waiting")


def _verdict(e) -> dict | None:
    """Mint's verdict on a finished run that is a test run, or None."""
    if e.get("kind") != "run" or not _finished(e) or not at.is_test(_command(e)):
        return None
    ran, code, interrupted = _exit(e)
    if not ran:
        return {"state": "unclear", "reason": ""}       # it never ran: agent_watch shows "didn't finish"
    return at.verdict(_command(e), _output(e), code, interrupted)


def _patch_lines(e) -> list:
    out = []
    for raw in str((e.get("body") or {}).get("patch") or "").split("\n"):
        if raw[:1] in ("+", "-"):
            out.append((raw[0], None, raw[1:]))
    return out


def _basename(path: str) -> str:
    return re.split(r"[\\/]", str(path).rstrip("\\/"))[-1]


def claims(turn: dict) -> dict:
    """What Mint says about a request: {"tests", "why", "weakened", "risky", "retries"} like dotpals' claimsOf."""
    steps = sorted(turn["steps"], key=lambda e: e.get("at") or 0)
    verdicts = {e["id"]: v for e in steps if (v := _verdict(e))}
    tests = {i: v["state"] for i, v in verdicts.items()}
    why = {i: (v.get("reason") or None) for i, v in verdicts.items() if v["state"] != "passed"}

    weak, last = None, None
    risky: list[str] = []
    for e in steps:
        if e["id"] in verdicts:
            last = verdicts[e["id"]]["state"]
        if e.get("status") != "failed":
            for f in e.get("files") or []:
                if f.get("change") == "read":
                    continue
                flag = at.risk("Delete" if f.get("change") == "delete" else "Edit", _basename(f["path"]),
                               path=f["path"])
                text = mapped_risk(flag) if flag else None
                if text and text not in risky:
                    risky.append(text)
        if e.get("kind") == "run" and _exit(e)[0]:
            flag = at.risk("Run", _command(e), cmd=_command(e))
            text = mapped_risk(flag) if flag else None
            if text and text not in risky:
                risky.append(text)
        if e.get("kind") in ("edit", "write") and e.get("status") != "failed" and last == "failed":
            for f in e.get("files") or []:
                if at.is_test_file(f.get("path")):
                    w = at.weakened(_patch_lines(e), f["path"])
                    if w:
                        weak = f"{w} in {_basename(f['path'])}"
    return {"tests": tests, "why": why, "weakened": weak, "risky": sorted(risky), "retries": retries(steps, verdicts)}


def retries(steps: list, verdicts: dict) -> dict:
    """{failed step id: "fixed" | "failing"} for each failed run the agent tried again (same_command)."""
    steps = [e for e in steps if e.get("kind") not in ("prompt", "done", "error", "plan", "compact")]

    def failed(e) -> bool:
        if e.get("kind") != "run":
            return False
        v = verdicts.get(e["id"])
        if v is not None:
            return v["state"] == "failed"
        ran, code, _ = _exit(e)
        return ran and code not in (0, None)

    out, retried = {}, set()
    for i, first in enumerate(steps):
        if not failed(first) or first["id"] in retried:
            continue
        attempts = [first]
        for e in steps[i + 1:i + 26]:
            if (e.get("at") or 0) - (attempts[-1].get("at") or 0) > 15 * 60_000:
                break
            if e.get("kind") != "run" or not at.same_command(_command(first), _command(e)):
                continue
            if verdicts.get(e["id"], {}).get("state") == "unclear":
                continue                                # a run that can't tell neither fixes nor fails
            attempts.append(e)
            retried.add(e["id"])
            if not failed(e):
                break
        if first["id"] in verdicts and failed(attempts[-1]):
            k = steps.index(attempts[-1])
            for e in steps[k + 1:k + 61]:
                if (e.get("at") or 0) - (attempts[-1].get("at") or 0) > 15 * 60_000:
                    break
                if verdicts.get(e["id"], {}).get("state") == "passed" and at.whole_suite(_command(e)):
                    attempts.append(e)
                    retried.add(e["id"])
                    break
        if len(attempts) > 1:
            out[first["id"]] = "failing" if failed(attempts[-1]) else "fixed"
    return out


# -- scoring ----------------------------------------------------------------------------------------------------------

def _plain(text: str) -> str:
    """A reason without its error-class label ("AssertionError [ERR_ASSERTION]: "), its location ("(test/x.js:5)"), a
    trailing "…" and extra spaces, in lower case."""
    s = " ".join(str(text).split()).rstrip("…").strip()
    s = re.sub(r"\s*\((?:[^()]*[\\/])?[^()\s]+:\d+\)$", "", s)
    s = re.sub(r"^\w*Error(?: \[[A-Z_]+\])?:\s*", "", s)
    return s.lower()


def _why_matches(said: str, truth: str) -> bool:
    """The truth's first meaningful fragment (its first 30 characters once the error-class label is gone) is in Mint's
    reason, or Mint's reason (12 characters or more, label and location gone) is in the truth: the two name the same
    failure, whichever of them was cut shorter."""
    s, t = _plain(said), _plain(truth)
    if not s or not t:
        return False
    return t[:30] in s or (len(s) >= 12 and s in t)


_WEAK_KINDS = (("removed", r"\bremoved\b"), ("skip", r"\bskip|\boff\b"), ("changed", r"\bchanged what\b"))


def _weak_kinds(text) -> frozenset:
    return frozenset(k for k, rx in _WEAK_KINDS if re.search(rx, str(text or "")))


def score_case(case: dict) -> list[dict]:
    said, truth = claims(case["turn"]), case["truth"]
    out = []

    def add(kind, id_, s, t, result):
        out.append({"kind": kind, "id": id_, "said": s, "truth": t, "result": result})

    for id_ in sorted({*truth.get("tests", {}), *said["tests"]}, key=_step_order):
        s, t = said["tests"].get(id_), truth.get("tests", {}).get(id_)
        add("tests", id_, s, t, "right" if s == t else "unsure" if s in (None, "unclear") else "wrong")
    for id_, t in sorted((truth.get("why") or {}).items(), key=lambda kv: _step_order(kv[0])):
        s = said["why"].get(id_)
        if t is None:
            add("why", id_, s, t, "right" if s is None else "wrong")
        else:
            add("why", id_, s, t, "unsure" if s is None else "right" if _why_matches(s, t) else "wrong")
    if "weakened" in truth:
        s, t = said["weakened"], truth["weakened"]
        same = (s is None and t is None) or (s is not None and t is not None and _weak_kinds(s) == _weak_kinds(t))
        add("weakened", "weakened", s, t, "right" if same else "wrong")
    if "risky" in truth:
        s_set = set(said["risky"])
        t_set = {x for x in truth["risky"] if _DOTPALS_WARNINGS.match(x)}
        for x in sorted(s_set | t_set):
            add("risky", x, x in s_set, x in t_set, "right" if (x in s_set) == (x in t_set) else "wrong")
    for id_ in sorted({*truth.get("retries", {}), *said["retries"]}, key=_step_order):
        s, t = said["retries"].get(id_), truth.get("retries", {}).get(id_)
        add("retries", id_, s, t, "right" if s == t else "wrong")
    return out


def _step_order(id_: str):
    m = re.match(r"e(\d+)$", str(id_))
    return (0, int(m[1]), "") if m else (1, 0, str(id_))


def load_cases(folder: Path = CASES) -> list[dict]:
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
    return [c for c in cases if c.get("labeled") is True]


def score_all(cases: list[dict]) -> dict:
    total = {"right": 0, "wrong": 0, "unsure": 0}
    by_kind = {k: {"right": 0, "wrong": 0, "unsure": 0} for k in KINDS}
    wrong, unsure = [], []
    for c in cases:
        for r in score_case(c):
            total[r["result"]] += 1
            by_kind[r["kind"]][r["result"]] += 1
            if r["result"] != "right":
                (wrong if r["result"] == "wrong" else unsure).append(
                    {"case": c["name"], "kind": r["kind"], "id": r["id"], "said": r["said"], "truth": r["truth"]})
    return {"cases": len(cases), "total": total, "byKind": by_kind, "wrong": wrong, "unsure": unsure}


def key(w: dict) -> str:
    return f"{w['case']} {w['kind']} {w['id']}"


def baseline_of(score: dict) -> dict:
    return {"total": score["total"], "byKind": score["byKind"], "wrong": [key(w) for w in score["wrong"]],
            "unsure": [key(w) for w in score["unsure"]]}


def table(score: dict) -> str:
    def all_(k):
        return k["right"] + k["wrong"] + k["unsure"]

    def pct(n, d):
        return f"{100 * n / d:.1f}%" if d else "-"

    rows = [f"Mint accuracy: {score['cases']} real requests, {all_(score['total'])} claims checked by hand", "",
            f"{'Claim':<18}{'Claims':>8}{'Right':>8}{'Wrong':>8}{'Unsure':>8}{'Accuracy':>10}"]
    for kind, k in [*score["byKind"].items(), ("All", score["total"])]:
        rows.append(f"{LABEL.get(kind, kind):<18}{all_(k):>8}{k['right']:>8}{k['wrong']:>8}{k['unsure']:>8}"
                    f"{pct(k['right'], all_(k)):>10}")
    rows += ["", "Accuracy = right / all. Unsure: a test run Mint didn't read as one or called unclear though a person "
                 "can tell,", "or a failure it gave no reason for though the output shows one (a missing answer, not a "
                 "false one)."]
    for name, items in (("Wrong", score["wrong"]), ("Unsure", score["unsure"])):
        if items:
            rows += ["", f"{name} ({len(items)}):"]
            rows += [f"  {key(w)}: said {json.dumps(w['said'], ensure_ascii=False)[:160]}, really "
                     f"{json.dumps(w['truth'], ensure_ascii=False)[:160]}" for w in items]
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    score = score_all(load_cases())
    if "--save" in argv:
        BASELINE.write_text(json.dumps(baseline_of(score), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Saved as the baseline ({BASELINE.name}).\n")
    print(json.dumps(score, indent=2, ensure_ascii=False) if "--json" in argv else table(score))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
