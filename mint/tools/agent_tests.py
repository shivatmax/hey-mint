"""What coding agents' test runs really say. Mint reads Claude Code's and Codex's session logs (agent_watch.py); this
module reads the steps in them: what a test command's output says (passed, failed, skipped, and why the first
failure failed), whether a run can be trusted at all (nothing ran, output cut by a pipe, no exit code), risky steps
(a changed .env, a force-push, rm -rf, curl | sh, sudo ...) and passes faked by weakening a test (an assertion taken
out, a skip added, an expected value changed).

Plain rules, no model: instant, free and the same every time. Only the structured lines each runner prints count
(summaries, FAIL markers, failing test ids); prose in a log is ignored. The worst case wins: a failure marker anywhere
beats a passing summary, and output cut off before its summary never counts as a pass. Pure stdlib, any thread.
Everything read here is data from the logs, never an instruction to Mint.

Parsers and rules ported from dotpals (MIT, (c) 2026 dotpals contributors), which adapted parts from claude-referee
(MIT): dotpals' bridge/ui/testout.js (the test-output parsers and the failure reason) and bridge/ui/story.js (test,
ship and pipe detection, verdicts, risky-command and secret flags, weakened tests).

API:
    is_test(cmd) / is_check(cmd)         the command runs tests / a linter or type checker
    ships(cmd)                           it commits, pushes, opens a PR or publishes, and its tests can't stop it
    piped(cmd)                           the tests' output goes through a pipe (| tail, | grep ...)
    parse(output)                        {"framework", "passed", "failed", "skipped", "total", "errors", "failing"}
    failure_reason(output)               "expected 3, got -1 (test/math.test.js:5)" or ""
    verdict(cmd, output, exit_code)      {"state": passed / failed / unclear, "line": "2 of 48 failed", ...}
    risk(verb, target, cmd, path)        "changed .env", "force-push", "rm -rf" ... or ""
    weakened(lines, path)                "removed an assertion" ... for an edit to a test file, or ""
    same_command(a, b)                   two runs of the same command (linking retries)
    whole_suite(cmd)                     a test run with no file or name filter
    only_old_failures(before, now)       a failing run fails only what already failed before the agent's changes
"""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")
_MAX_TEXT = 500_000         # a huge log: its end, where the summary is
_MAX_LINE = 2_000
_MAX_FAILING = 10
_MAX_NAME = 120
_WHY_MAX = 140


def _lines(text) -> list[str]:
    text = _ANSI.sub("", str(text or "")[-_MAX_TEXT:])
    return [ln[:_MAX_LINE] for ln in re.split(r"\r\n|\r|\n", text)]


def _names(ids) -> list[str]:
    out: list[str] = []
    for raw in ids:
        name = str(raw).strip()[:_MAX_NAME]
        if name and name not in out:
            out.append(name)
        if len(out) == _MAX_FAILING:
            break
    return out


def _facts(runner: str, passed=0, failed=0, errors=0, skipped=0, failing=(), lint=False) -> dict:
    return {"runner": runner, "passed": passed, "failed": failed, "errors": errors, "skipped": skipped,
            "failing": _names(failing), "lint": lint}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# -- JavaScript: jest, vitest, mocha, node:test, eslint, tsc ------------------------------------------------------

def _counts_of(body: str) -> dict:
    """"1 failed, 5 passed, 2 skipped, 8 total" -> counts."""
    c = {"passed": 0, "failed": 0, "skipped": 0}
    for n, word in re.findall(r"(\d+)\s+(failed|passed|skipped|todo|pending|total)\b", body):
        if word in ("failed", "passed"):
            c[word] += int(n)
        elif word != "total":
            c["skipped"] += int(n)
    return c


def _tally(lines, pattern):
    """The last line matching `pattern` (its counts) and the most failures any such line reported."""
    line, last, failed_max = None, {"passed": 0, "failed": 0, "skipped": 0}, 0
    for ln in lines:
        m = re.match(pattern, ln)
        if m:
            line, last = ln, _counts_of(m[1] or "")
            failed_max = max(failed_max, last["failed"])
    return line, last, failed_max


def _jest(lines):
    t_line, tests, t_failed = _tally(lines, r"\s*Tests:\s+(\d.*)$")
    s_line, _, s_failed = _tally(lines, r"\s*Test Suites:\s+(\d.*)$")
    no_tests = any(re.match(r"\s*No tests found\b", ln) for ln in lines)
    ids, files, run_errors = {}, {}, 0
    for ln in lines:
        h = re.match(r"\s*● (.+?)\s*$", ln)
        if h:
            if re.match(r"Test suite failed to run\b", h[1]):
                run_errors += 1
            elif not re.match(r"(?:Console|Validation Warning|Deprecation Warning)\b", h[1]):
                ids[h[1]] = None
            continue
        f = re.match(r"\s*FAIL\s+(\S*[./]\S*)(?:\s+\(.*\))?\s*$", ln)
        if f:
            files[f[1]] = None
    if t_line is None and s_line is None and not no_tests and not ids and not files and not run_errors:
        return None
    failed = max(t_failed, len(ids), len(files) if not ids and not run_errors else 0)
    errors = max(run_errors, s_failed if failed == 0 else 0)
    return _facts("jest", tests["passed"], failed, errors, tests["skipped"], ids or files)


def _vitest(lines):
    t_line, tests, t_failed = _tally(lines, r"\s*Tests\s+(\d.*)$")
    f_line, _, f_failed = _tally(lines, r"\s*Test Files\s+(\d.*)$")
    no_files = any(re.match(r"\s*No test files found\b", ln) for ln in lines)
    ids, load_fails, marked, unhandled = {}, {}, {}, 0
    for ln in lines:
        f = re.match(r"\s*FAIL\s+(\S.*?)\s*$", ln)
        if f:
            if " > " in f[1]:
                ids[f[1]] = None
            elif re.search(r"\[.*\]$", f[1]):
                load_fails[f[1]] = None
            continue
        m = re.match(r"\s*❯ (\S+) \(\d+ tests?(?: \| (\d+) failed)?", ln)
        if m and int(m[2] or 0) > 0:
            marked[m[1]] = None
            continue
        e = re.match(r"\s*Errors\s+(\d+) errors?\b", ln)
        if e:
            unhandled = max(unhandled, int(e[1]))
    if t_line is None and f_line is None and not no_files and not (ids or load_fails or marked) and not unhandled:
        return None
    failed = max(t_failed, len(ids), len(marked) if not ids else 0)
    errors = max(len(load_fails), unhandled, f_failed if failed == 0 else 0)
    failing = [*ids, *load_fails] if ids or load_fails else marked
    return _facts("vitest", tests["passed"], failed, errors, tests["skipped"], failing)


def _mocha(lines):
    pass_i = pend_i = fail_i = -1
    passed = skipped = failed_max = 0
    ticks = False
    for i, ln in enumerate(lines):
        m = re.match(r"\s*(\d+) (passing|failing|pending)\b", ln)
        if m:
            n = int(m[1])
            if m[2] == "passing":
                pass_i, passed = i, n
            elif m[2] == "pending":
                pend_i, skipped = i, n
            else:
                fail_i, failed_max = i, max(failed_max, n)
        elif re.match(r"\s*[✔✓]\s", ln):
            ticks = True
    if pend_i < pass_i:
        skipped = 0
    summary = pass_i >= 0 or fail_i >= 0 or pend_i >= 0
    ids: dict[int, str] = {}
    if summary or ticks:
        for i, ln in enumerate(lines):
            m = re.match(r"\s{2,}(\d+)\) (\S.*?)\s*$", ln)
            if not m:
                continue
            name = m[2]
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            under = re.match(r"\s{5,}(\S.*):\s*$", nxt) if 0 <= fail_i < i else None
            ids[int(m[1])] = f"{name} > {under[1]}" if under else name
    if not summary and not ids:
        return None
    return _facts("mocha", passed, max(failed_max, len(ids)), 0, skipped, ids.values())


def _node_test(lines):
    """node --test: "ℹ tests 5 / ℹ pass 5 / ℹ fail 0", the TAP reporter's "# pass 5", and a summary without its
    "tests" line ("... | grep pass")."""
    def num(key):
        rx = re.compile(rf"\s*(?:ℹ|#) {key} (\d+)\s*$")
        for ln in reversed(lines):
            m = rx.match(ln)
            if m:
                return int(m[1])
        return None

    passes, fails = num("pass"), num("fail")
    ids, in_failing = {}, False
    for ln in lines:
        if re.match(r"✖ failing tests:\s*$", ln):
            in_failing = True
        m = re.match(r"✖ (.+?) \(\d+(?:\.\d+)?ms\)\s*$", ln)
        if m and not in_failing:
            ids[m[1]] = None
        tap = re.match(r"not ok \d+ - (.+?)\s*(?:#.*)?$", ln)
        if tap:
            ids[tap[1]] = None
    failed = max(fails or 0, len(ids))
    if passes is None and fails is None and failed == 0:
        return None
    return _facts("node:test", passes or 0, failed, 0, (num("skipped") or 0) + (num("todo") or 0), ids)


def _eslint(lines):
    summary, errors_max, file, entries = False, 0, "", {}
    for ln in lines:
        s = re.match(r"\s*✖\s+(\d+) problems?\s+\((\d+) errors?,\s*(\d+) warnings?\)", ln)
        if s:
            summary, errors_max = True, max(errors_max, int(s[2]))
            continue
        d = re.match(r"\s+(\d+):(\d+)\s+(error|warning)\s+(.*?)(?:\s{2,}([@\w/.-]+))?\s*$", ln)
        if d:
            if d[3] == "error":
                entries[f"{file + ':' if file else ''}{d[1]}:{d[2]} {d[5] or 'error'}"] = None
            summary = True
            continue
        if re.match(r"\S*[./\\]\S*$", ln):
            file = ln
    if not summary:
        return None
    return _facts("eslint", errors=max(errors_max, len(entries)), failing=entries, lint=True)


def _tsc(lines):
    summary, errors_max, entries = False, 0, {}
    for ln in lines:
        s = re.match(r"\s*(?:\[[^\]]*\]\s*)?Found (\d+) errors?\b", ln)
        if s:
            summary, errors_max = True, max(errors_max, int(s[1]))
            continue
        a = (re.match(r"\s*(\S+?)\((\d+),(\d+)\):\s+error\s+(TS\d+):", ln)
             or re.match(r"\s*(\S+?):(\d+):(\d+)\s+-\s+error\s+(TS\d+):", ln))
        if a:
            entries[f"{a[1]}:{a[2]}:{a[3]} {a[4]}"] = None
            continue
        g = re.match(r"\s*error\s+(TS\d+):", ln)
        if g:
            entries[g[1]] = None
    if not summary and not entries:
        return None
    return _facts("tsc", errors=max(errors_max, len(entries)), failing=entries, lint=True)


# -- Python: pytest, unittest, ruff ----------------------------------------------------------------------------------

_TIME = r"in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?"
_PYTEST_SUMMARY = re.compile(
    rf"(?:\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?|rerun)(?:, )?)+ {_TIME}$")
_PYTEST_NO_TESTS = re.compile(rf"no tests ran {_TIME}$")
_PYTEST_EMPTY = re.compile(r"(?:=+\s*|collecting \.\.\. )?collected 0 items\b")
_PYTEST_ID = re.compile(r"[\w./\\-]+\.py(?:::\S.*)?$")


def _pytest_counts(line: str) -> dict:
    out = {"failed": 0, "passed": 0, "skipped": 0, "errors": 0}
    for n, word in re.findall(r"(\d+) (failed|passed|skipped|errors?)\b", line):
        key = "errors" if word.startswith("error") else word
        out[key] = max(out[key], int(n))
    return out


def _pytest(lines):
    failed_ids, error_ids, summaries = {}, {}, []
    empty = failures_block = errors_block = False
    for raw in lines:
        line = raw.strip()
        bare = re.sub(r"^=+\s*|\s*=+$", "", line)
        if _PYTEST_SUMMARY.match(bare) or _PYTEST_NO_TESTS.match(bare):
            summaries.append(line)
            continue
        if _PYTEST_EMPTY.match(line):
            empty = True
            continue
        if re.match(r"=+ FAILURES =+$", line):
            failures_block = True
        elif re.match(r"=+ ERRORS =+$", line):
            errors_block = True
        collect = re.match(r"_+ ERROR collecting (\S+\.py) _+$", line)
        if collect:
            error_ids[collect[1]] = None
            continue
        short = re.match(r"(?:\[gw\d+\]\s+)?(?:\[\s*\d+%\]\s+)?(FAILED|ERROR)\s+(.+)$", line)
        if short:
            test_id = short[2].split(" - ")[0].strip()
            if _PYTEST_ID.match(test_id):
                (failed_ids if short[1] == "FAILED" else error_ids)[test_id] = None
            continue
        verbose = re.match(r"([\w./\\-]+\.py::\S.*?)\s+(FAILED|ERROR)\b", line)
        if verbose:
            (failed_ids if verbose[2] == "FAILED" else error_ids)[verbose[1]] = None
    markers = failed_ids or error_ids or failures_block or errors_block
    if not summaries and not empty and not markers:
        return None
    failed = max(len(failed_ids), 1 if failures_block else 0)
    errors = max(len(error_ids), 1 if errors_block else 0)
    for s in summaries:
        c = _pytest_counts(s)
        failed, errors = max(failed, c["failed"]), max(errors, c["errors"])
    tail = _pytest_counts(summaries[-1] if summaries else "")
    return _facts("pytest", tail["passed"], failed, errors, tail["skipped"], [*failed_ids, *error_ids])


def _unittest(lines):
    """python -m unittest: "Ran 5 tests in 0.01s", then "OK" or "FAILED (failures=1)"."""
    ran, result, ids = None, None, {}
    for raw in lines:
        line = raw.strip()
        r = re.match(r"Ran (\d+) tests? in \d", line)
        if r:
            ran = int(r[1])
            continue
        o = re.match(r"(OK|FAILED)(?: \((.*)\))?$", line)
        if o and ran is not None:
            result = (o[1] == "OK", o[2] or "")
            continue
        f = re.match(r"(?:FAIL|ERROR): (\S+) \((\S+)\)", line)
        if f:
            ids[f"{f[2]}.{f[1]}"] = None
    if ran is None:
        return None

    def count(key):
        m = re.search(rf"\b{key}=(\d+)", result[1] if result else "")
        return int(m[1]) if m else 0

    failed = max(count("failures"), 1 if result and not result[0] and not count("errors") else 0)
    errors = count("errors")
    skipped = count("skipped") + count("expected failures")
    # No OK / FAILED line yet: cut off before the result, so nothing passed for sure.
    passed = max(0, ran - failed - errors - skipped) if result else 0
    return _facts("unittest", passed, failed, errors, skipped, ids)


def _ruff(lines):
    violations, found, fixable, clean, header = {}, 0, 0, False, None
    for raw in lines:
        line = raw.strip()
        f = re.match(r"Found (\d+) errors?\.$", line)
        if f:
            found = max(found, int(f[1]))
            continue
        if line == "All checks passed!":
            clean = True
            continue
        fix = re.match(r"\[\*\] (\d+) fixable with the .{0,4}--fix.{0,4} option", line)
        if fix:
            fixable = max(fixable, int(fix[1]))
            continue
        concise = re.match(r"(\S+?):(\d+):(\d+): ([A-Z]{1,4}\d{2,4})(?: |$)", raw.lstrip())
        if concise:
            violations[f"{concise[1]}:{concise[2]}:{concise[3]} {concise[4]}"] = None
            continue
        head = re.match(r"([A-Z]{1,4}\d{2,4}) (?:\[\*\] )?\S", line)
        if head:
            header = head[1]
            continue
        arrow = re.match(r"\s*--> (\S+?):(\d+):(\d+)$", raw)
        if arrow and header:
            violations[f"{arrow[1]}:{arrow[2]}:{arrow[3]} {header}"] = None
            header = None
    if not clean and not found and not fixable and not violations:
        return None
    return _facts("ruff", errors=max(found, len(violations), fixable), failing=violations, lint=True)


# -- compiled: go test, cargo test, dotnet test, Maven, Gradle ------------------------------------------------------

def _top(ids) -> int:
    """Subtests (TestX/sub) are reported next to their parent, so only top-level ids count."""
    top = sum(1 for n in ids if "/" not in n)
    return top or len(ids)


def _go_test(lines):
    summaries = ok_pkgs = failed_pkgs = build_failed = 0
    failed_ids, passed_ids, skipped_ids, started = {}, {}, {}, {}
    other = panic = goroutine = bare_fail = False
    for ln in lines:
        ok = re.match(r"ok\s+\S+\s+(?:[\d.]+s\b|\(cached\))(.*)$", ln)
        if ok:
            summaries += 1
            ok_pkgs += "[no tests to run]" not in (ok[1] or "")
            continue
        if re.match(r"FAIL\s+\S+\s+(?:[\d.]+s\b|\[(?:build|setup) failed\])", ln):
            summaries += 1
            if re.search(r"\[(?:build|setup) failed\]", ln):
                build_failed += 1
            else:
                failed_pkgs += 1
            continue
        if re.match(r"\?\s+\S+\s+\[no test files\]", ln):
            summaries += 1
            continue
        t = re.match(r"\s*--- (FAIL|PASS|SKIP): (\S+)", ln)
        if t:
            other = True
            {"FAIL": failed_ids, "PASS": passed_ids, "SKIP": skipped_ids}[t[1]][t[2]] = None
            continue
        run = re.match(r"\s*=== RUN\s+(\S+)", ln)
        if run:
            started[run[1]] = None
        if re.match(r"\s*=== (?:RUN|PAUSE|CONT)\s", ln):
            other = True
        elif ln.startswith("panic: "):
            panic = True
        elif re.match(r"goroutine \d+ \[", ln):
            goroutine = True
        elif re.match(r"FAIL\s*$", ln):
            bare_fail = True
    if not summaries and not other and not (panic and goroutine):
        return None
    unfinished = any(i not in failed_ids and i not in passed_ids and i not in skipped_ids for i in started)
    errors = build_failed + int(unfinished) + (1 if panic and (goroutine or other or summaries) else 0)
    failed = max(failed_pkgs, _top(failed_ids), 1 if bare_fail and not errors else 0)
    if not summaries and not failed and not errors:
        return None
    return _facts("go test", max(ok_pkgs, _top(passed_ids)), failed, errors, _top(skipped_ids), failed_ids)


def _cargo_test(lines):
    results = passed = failed_sum = ignored = ok_lines = 0
    failed_ids, compile_errors = {}, {}
    failed_status = could_not_compile = test_failed_line = False
    for i, ln in enumerate(lines):
        # "ignored" is optional: clipped and filtered output too.
        r = re.match(r"test result: (ok|FAILED)\.\s*(\d+) passed;\s*(\d+) failed;(?:\s*(\d+) ignored;)?", ln)
        if r:
            results += 1
            passed, failed_sum, ignored = passed + int(r[2]), failed_sum + int(r[3]), ignored + int(r[4] or 0)
            failed_status = failed_status or r[1] == "FAILED"
            continue
        t = re.match(r"test (.+?) \.\.\. (ok|FAILED|ignored)\b", ln)
        if t:
            if t[2] == "FAILED":
                failed_ids[t[1]] = None
            elif t[2] == "ok":
                ok_lines += 1
            continue
        s = re.match(r"---- (.+?) stdout ----$", ln)
        if s:
            failed_ids[s[1]] = None
            continue
        if re.match(r"failures:\s*$", ln):
            for item in lines[i + 1:]:
                m = re.match(r" {4}([\w:]+|\S+ - .+ \(line \d+\))$", item)
                if not m:
                    break
                failed_ids[m[1]] = None
            continue
        if re.match(r"error\[E\d+\]", ln):
            compile_errors[ln] = None
        elif ln.startswith("error: could not compile "):
            could_not_compile = True
        elif ln.startswith("error: test failed, to rerun pass"):
            test_failed_line = True
    errors = len(compile_errors) or int(could_not_compile)
    failed = max(failed_sum, len(failed_ids), 1 if failed_status or test_failed_line else 0)
    if not results and not failed and not errors:
        return None
    return _facts("cargo test", passed if results else ok_lines, failed, errors, ignored, failed_ids)


def _dotnet_test(lines):
    summaries = passed = failed_sum = skipped = passed_lines = 0
    failed_ids, build_errors = {}, {}
    failed_status = no_tests = run_failed = build_failed = False
    for ln in lines:
        s = re.match(r"\s*(Passed|Failed)!\s+-\s+Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+),"
                     r"\s*Total:\s*(\d+)", ln)
        if s:
            summaries += 1
            failed_sum, passed, skipped = failed_sum + int(s[2]), passed + int(s[3]), skipped + int(s[4])
            failed_status = failed_status or s[1] == "Failed"
            continue
        f = re.match(r"\s+Failed (.+?) \[[^\]]*\]\s*$", ln)
        if f:
            failed_ids[f[1]] = None
            continue
        if re.match(r"\s+Passed (.+?) \[[^\]]*\]\s*$", ln):
            passed_lines += 1
            continue
        # MSBuild and compiler codes (CS0103, MSB3073, NETSDK1045), not TypeScript's (TS2322).
        if re.search(r"\berror (?!TS\d)[A-Z]{2,6}\d{3,5}:", ln):
            build_errors[re.match(r"(.*?)\s*(?:\[[^\]]*\.\w*proj\])?\s*$", ln)[1].strip()] = None
            continue
        if re.match(r"\s*No test is available\b", ln):
            no_tests = True
        elif re.match(r"\s*Test Run Failed\.?\s*$", ln):
            run_failed = True
        elif re.match(r"\s*Build FAILED\.?\s*$", ln):
            build_failed = True
    errors = len(build_errors) or int(build_failed)
    failed = max(failed_sum, len(failed_ids), 1 if failed_status or run_failed else 0)
    if not summaries and not no_tests and not failed and not errors:
        return None
    return _facts("dotnet test", passed if summaries else passed_lines, failed, errors, skipped, failed_ids)


def _maven(lines):
    """Maven Surefire: "Tests run: 5, Failures: 1, Errors: 0, Skipped: 0". The last one is the total."""
    last, failed_max, errors_max, ids = None, 0, 0, {}
    for ln in lines:
        m = re.match(r"(?:\[\w+\]\s+)?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
                     ln)
        if m:
            last = [int(x) for x in m.groups()]
            failed_max, errors_max = max(failed_max, last[1]), max(errors_max, last[2])
            continue
        f = re.match(r"\[ERROR\]\s+(\S+?[.#]\S+?)(?::\d+)?\s+(?:»|<<<)\s", ln)
        if f:
            ids[f[1]] = None
    if not last:
        return None
    run, failed, errors, skipped = last
    return _facts("maven", max(0, run - failed - errors - skipped), max(failed, failed_max), max(errors, errors_max),
                  skipped, ids)


def _gradle(lines):
    """Gradle: "5 tests completed, 1 failed, 1 skipped" and "> Task :test FAILED"."""
    summary, task_failed, ids = None, False, {}
    for ln in lines:
        m = re.match(r"\s*(\d+) tests? completed(?:, (\d+) failed)?(?:, (\d+) skipped)?", ln)
        if m:
            summary = [int(x or 0) for x in m.groups()]
            continue
        if re.match(r"> Task :\S*test\S* FAILED$", ln.strip(), re.I):
            task_failed = True
        f = re.match(r"(\S+) > (.+?) FAILED$", ln.strip())
        if f:
            ids[f"{f[1]} > {f[2]}"] = None
    if not summary and not ids:
        return None
    done, failed, skipped = summary or (0, 0, 0)
    failed = max(failed, len(ids), 1 if task_failed and not summary else 0)
    return _facts("gradle", max(0, done - failed - skipped), failed, 0, skipped, ids)


def _deno_bun(lines):
    """Deno ("ok | 5 passed | 0 failed") and Bun (" 5 pass", " 0 fail")."""
    deno, passes, fails, skips = None, None, None, 0
    for ln in lines:
        d = re.match(r"(ok|FAILED) \| (\d+) passed(?: \((\d+) steps?\))? \| (\d+) failed(?: \((\d+) steps?\))?"
                     r"(?: \| (\d+) ignored)?", ln.strip())
        if d:
            deno = (int(d[2]), int(d[4]), int(d[6] or 0))
            continue
        b = re.match(r"\s*(\d+) (pass|fail|skip|todo)\s*$", ln)
        if b:
            if b[2] == "pass":
                passes = int(b[1])
            elif b[2] == "fail":
                fails = int(b[1])
            else:
                skips += int(b[1])
    if deno:
        return _facts("deno", deno[0], deno[1], 0, deno[2])
    if passes is not None and fails is not None:
        return _facts("bun", passes, fails, 0, skips)
    return None


# -- PHP and Ruby: PHPUnit, RSpec ---------------------------------------------------------------------------------

def _phpunit(lines):
    candidates, failure_ids, error_ids = [], {}, {}
    section = None
    sum_failures = sum_errors = head_failures = head_errors = fail_floor = err_floor = 0
    progress_fail = False

    def counts(rest):
        out: dict[str, int] = {}
        for key, n in re.findall(r"([A-Za-z][A-Za-z ]*?):\s*(\d+)", rest):
            out[key.lower()] = max(out.get(key.lower(), 0), int(n))
        return out

    for line in (ln.rstrip() for ln in lines):
        if m := re.match(r"OK \((\d+) tests?, (\d+) assertions?\)", line):
            candidates.append((int(m[1]), 0))
            section = None
        elif re.match(r"OK, but .*!$", line):
            candidates.append((0, 0))
            section = None
        elif m := re.match(r"Tests:\s*(\d+)\s*(?:,(.*?))?\.?$", line):
            c = counts(m[2] or "")
            failures, errors, skipped = c.get("failures", 0), c.get("errors", 0), c.get("skipped", 0)
            sum_failures, sum_errors = max(sum_failures, failures), max(sum_errors, errors)
            candidates.append((max(0, int(m[1]) - failures - errors - skipped), skipped))
            section = None
        elif re.match(r"No tests executed!", line):
            candidates.append((0, 0))
            section = None
        elif line == "FAILURES!":
            fail_floor, section = 1, None
        elif line == "ERRORS!":
            err_floor, section = 1, None
        elif m := re.match(r"There (?:was|were) (\d+) ([a-z ]+?)s?:$", line, re.I):
            kind = m[2].lower()
            if kind == "failure":
                section, head_failures = "failure", max(head_failures, int(m[1]))
            elif kind == "error":
                section, head_errors = "error", max(head_errors, int(m[1]))
            else:
                section = "other"
        elif section in ("failure", "error"):
            if m := re.match(r"(\d+)\) ([\w\\]+::\S.*)$", line):
                (failure_ids if section == "failure" else error_ids)[f"{m[1]}) {m[2]}"] = m[2]
        elif re.match(r"[.FEWSIRDN]+\s+\d+ / \d+ \(\s*\d+%\)$", line.strip()):
            if re.search(r"[FE]", line.strip().split()[0]):
                progress_fail = True
    last = candidates[-1] if candidates else None
    failed = max(sum_failures, len(failure_ids), head_failures, fail_floor)
    errors = max(sum_errors, len(error_ids), head_errors, err_floor)
    if not failed and not errors and progress_fail:
        failed = 1
    if not last and not failed and errors:
        failed = errors
    if not last and not failed and not errors:
        return None
    passed, skipped = last or (0, 0)
    return _facts("phpunit", passed, failed, errors, skipped, [*failure_ids.values(), *error_ids.values()])


def _rspec(lines):
    summaries, numbered, located, load_errors = [], {}, {}, {}
    section = None
    sum_failures = sum_errors = 0
    failures_header = False
    for line in (ln.rstrip() for ln in lines):
        if m := re.match(r"\s*(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?"
                         r"(?:, (\d+) errors? occurred outside of examples)?\s*$", line):
            failures, pending = int(m[2]), int(m[3] or 0)
            sum_failures, sum_errors = max(sum_failures, failures), max(sum_errors, int(m[4] or 0))
            summaries.append((max(0, int(m[1]) - failures - pending), pending))
            section = None
        elif line == "Failures:":
            section, failures_header = "failures", True
        elif line == "Failed examples:":
            section = "failed"
        elif line == "Pending:":
            section = "pending"
        elif line.startswith("Finished in "):
            section = None
        elif m := re.match(r"rspec (\.?/?\S+?:\d+(?:\[[\d:]+\])?|\.?/\S+)\s*(?:#\s*(.*))?$", line):
            located[m[1]] = f"{m[1]} # {m[2]}" if m[2] else m[1]
        elif m := re.match(r"An error occurred while loading (\S+?)\.?$", line):
            load_errors[m[1]] = None
        elif section == "failures" and (m := re.match(r"\s*(\d+)\) (.+)$", line)):
            if lm := re.match(r"An error occurred while loading (\S+?)\.?$", m[2]):
                load_errors[lm[1]] = None
            numbered[f"{m[1]}) {m[2]}"] = m[2]
    last = summaries[-1] if summaries else None
    failure_ids = [n for n in numbered.values() if not n.startswith("An error occurred while loading ")]
    failed = max(sum_failures, len(failure_ids), len(located), 1 if failures_header and not load_errors else 0)
    errors = max(sum_errors, len(load_errors))
    if not last and not failed and not errors:
        return None
    passed, skipped = last or (0, 0)
    return _facts("rspec", passed, failed, errors, skipped, located.values() if located else numbered.values())


def _counted(lines):
    """Only when no runner above recognised the output: lines that start with a count ("  4 passed (3.0s)",
    "1 failed", "12 passing"), as Playwright, Cypress and many others print."""
    c, any_ = {"passed": 0, "failed": 0, "skipped": 0}, False
    for ln in lines:
        for n, word in re.findall(r"(?:^|[\s,|:(])(\d+)\s+(passed|passing|failed|failing|skipped|pending)\b", ln):
            any_ = True
            key = "passed" if word.startswith("pass") else "failed" if word.startswith("fail") else "skipped"
            c[key] = max(c[key], int(n))
    return _facts("tests", c["passed"], c["failed"], 0, c["skipped"]) if any_ else None


_RUNNERS = (_pytest, _unittest, _ruff, _jest, _vitest, _mocha, _eslint, _tsc, _node_test, _go_test, _cargo_test,
            _dotnet_test, _maven, _gradle, _deno_bun, _phpunit, _rspec)


def _parse(lines) -> dict | None:
    """Several runners in one output ("tsc && jest"): the one that counted the most tests speaks, and the failures or
    errors the others found are added (worst case wins). Linters and type checkers only ever add errors."""
    found = [f for f in (run(lines) for run in _RUNNERS) if f]
    if not any(not f["lint"] for f in found):
        fallback = _counted(lines)
        if fallback:
            found.append(fallback)
    if not found:
        return None
    size = lambda f: f["passed"] + f["failed"] + f["errors"] + f["skipped"]  # noqa: E731
    tests = sorted((f for f in found if not f["lint"]), key=lambda f: -size(f))
    lints = sorted((f for f in found if f["lint"]), key=lambda f: (-f["errors"], -len(f["failing"])))
    main = tests[0] if tests else {**lints[0], "errors": 0, "failing": []}
    out = {k: main[k] for k in ("runner", "passed", "failed", "errors", "skipped")}
    out["failing"], out["lint"] = list(main["failing"]), not tests

    def add(f):
        out["failing"] = _names([*out["failing"], *f["failing"]])
        if f["runner"] not in out["runner"]:
            out["runner"] += f" + {f['runner']}"

    for f in tests[1:]:
        if f["failed"] + f["errors"]:
            out["failed"], out["errors"] = max(out["failed"], f["failed"]), max(out["errors"], f["errors"])
            add(f)
    # Linters read the same lines in different ways ("Found 1 error." is tsc's and ruff's): count the most any found.
    if lints and lints[0]["errors"]:
        out["errors"] += lints[0]["errors"]
        add(lints[0])
    out["total"] = out["passed"] + out["failed"] + out["errors"] + out["skipped"]
    return out


def parse(output: str) -> dict | None:
    """{"framework", "passed", "failed", "skipped", "total", "errors", "failing": [names]} from a test command's
    output, or None when no runner's summary or failure markers were found."""
    f = _parse(_lines(output))
    if not f:
        return None
    return {"framework": f["runner"], "passed": f["passed"], "failed": f["failed"], "skipped": f["skipped"],
            "total": f["total"], "errors": f["errors"], "failing": f["failing"]}


# -- why a test failed ------------------------------------------------------------------------------------------------

# A source file and line, as stack traces and failure headers show them ("test/math.test.js:5:10"), and Python's
# traceback frames ('File "tests/test_x.py", line 12').
_WHERE = re.compile(r"((?:[\w.@-]+[\\/])*[\w.@-]+\.(?:[cm]?[jt]sx?|py|go|rs|rb|php|java|kt|cs|swift|exs?)):(\d+)")
_PY_WHERE = re.compile(r"File \"([^\"]+\.py)\", line (\d+)")
_TEST_SPECIFIC = re.compile(r"E\s+\S|[\w./\\-]+_test\.go:\d+:\s")


def _why(lines) -> tuple[str, str] | None:
    """Why the first failing test failed, in its runner's own words, and where: ("expected 3, got -1",
    "test/math.test.js:5"), or None. Only the first: for a person and for the agent, that's where to start."""
    lines = [re.sub(r"^\d+[-:]\s*", "", ln.strip()) for ln in lines]   # `grep -n` puts "160-" before each line

    def value(pattern):
        for ln in lines:
            m = re.match(pattern, ln)
            if m:
                return re.sub(r",$", "", m[1])
        return None

    # "Must not equal" or "must not match": expected and actual are the same thing, so its own words say it better.
    negated = any(re.match(r"operator:\s*'(?:not\w+|doesNot\w+)'", ln)
                  or (re.search(r"Error\b", ln) and re.search(r"\b(?:to not|not to|unequal)\b", ln, re.I))
                  for ln in lines)
    why = None
    if not negated:
        expected = value(r"Expected(?: value)?:\s+(.+)$")
        expected = value(r"expected:\s+(.+)$") if expected is None else expected
        actual = value(r"Received(?: value)?:\s+(.+)$")
        actual = value(r"actual:\s+(.+)$") if actual is None else actual
        if expected is not None and actual is not None:
            why = f"expected {expected}, got {actual}"
    if not why:
        # An error line can be another command's, crashed before the tests ran: look from the first failing test on.
        # Unless that's a test file that wouldn't load ("✖ test/x.test.js"): its error comes just before.
        at = next((i for i, ln in enumerate(lines)
                   if re.match(r"(?:✖|✗|×|●|not ok\b|FAIL\b|FAILED\b|--- FAIL\b|E\s+\S|\d+\) )", ln)), -1)
        loads = at > 0 and re.search(r"\.(?:test|spec)\.\w+\b|\btest_\w+\.py\b|_test\.\w+\b", lines[at])
        start = at if at > 0 and not loads else 0

        def reason_line(ln):
            return (_TEST_SPECIFIC.match(ln) or re.match(r"(?:\w+\s+)?\[?\w*(?:Assertion)?Error\b[^:]*:", ln)
                    or re.match(r"assertion\b.*\bfailed\b", ln, re.I))

        i = next((k for k in range(start, len(lines)) if reason_line(lines[k])), -1)
        if i < 0:   # go's -v output logs the reason before "--- FAIL"; these lines are never another command's
            i = next((k for k in range(start) if _TEST_SPECIFIC.match(lines[k])), -1)
        if i >= 0:
            why = re.sub(r"^[\w./\\-]+_test\.go:\d+:\s+", "", re.sub(r"^E\s+", "", lines[i]))
            # "Expected values to be strictly equal:" says what follows: the values.
            if why.endswith(":"):
                why = f"{why} {next((ln for ln in lines[i + 1:] if ln), '')}".strip()
    if not why:
        return None
    spots = [m for m in (_WHERE.search(ln) or _PY_WHERE.search(ln) for ln in lines)
             if m and not re.search(r"node_modules|node:|internal[\\/]|site-packages", m[0])]
    spot = next((m for m in spots if re.search(r"test|spec", m[1], re.I)), spots[0] if spots else None)
    where = "/".join(re.split(r"[\\/]", spot[1])[-2:]) + f":{spot[2]}" if spot else ""
    return why[:_WHY_MAX], where


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:max(1, n - 1)].rstrip() + "…"


def _reason_text(lines) -> str:
    r = _why(lines)
    if not r:
        return ""
    why, where = r
    if not where:
        return _cut(why, _WHY_MAX)
    return _cut(f"{_cut(why, max(20, _WHY_MAX - len(where) - 3))} ({where})", _WHY_MAX)


def failure_reason(output: str) -> str:
    """"expected 3, got -1 (test/math.test.js:5)", "assert -1 == 3 (tests/test_math.py:4)" ... (<= 140 chars), or ""."""
    return _reason_text(_lines(output))


# -- what a command is for --------------------------------------------------------------------------------------------

_TEST = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|jest|vitest|mocha)\b|\bnode\s+--test\b"
    r"|\b(?:jest|vitest|mocha|ava|pytest|py\.test|tox|nox|rspec|phpunit|pest|playwright\s+test|cypress\s+run)\b"
    r"|\bgo\s+test\b|\bcargo\s+(?:test|nextest)\b|\bdotnet\s+test\b|\bswift\s+test\b|\bdeno\s+test\b"
    r"|\bgradle\w*\b[^|;&\n]*?\s(?:\S*:)?test\w*\b|\bmvn\w*\b[^|;&\n]*?\s(?:test|verify)\b"
    r"|\bpython[\d.]*\s+-m\s+(?:pytest|unittest)\b|\bmake\s+test\b|\bctest\b|\bmix\s+test\b|\brake\s+test\b"
    r"|\b(?:flutter|dart)\s+test\b|\bxcodebuild\b[^|;&\n]*?\s(?:test|test-without-building)\b",
    re.I)
_CHECK = re.compile(
    r"\bruff\b(?!\s+format\b(?![^|;&\n]*\s--(?:check|diff)\b))|\b(?:eslint|tsc|vue-tsc|mypy|pyright|flake8|pylint"
    r"|biome|oxlint|golangci-lint|swiftlint|rubocop|shellcheck|stylelint)\b|\bprettier\b[^|;&\n]*\s--check\b"
    r"|\bcargo\s+(?:check|clippy)\b|\bgo\s+vet\b|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:lint|typecheck|type-check)\b",
    re.I)
# What puts work in front of others: a commit, a push (`git -C dir push` too), a pull request, a publish.
_SHIPS = re.compile(
    r"^git(?:\s+-[cC]\s+\S+|\s+--?[\w-]+(?:=\S+)?)*\s+(?:commit|push)\b"
    r"|^gh\s+(?:pr\s+(?:create|merge)|release\s+create)\b"
    r"|^(?:npm|pnpm|yarn|bun|cargo|poetry|uv)\s+publish\b|^twine\s+upload\b",
    re.I)
# Prefixes and wrappers a command is looked through (`sudo`, `xargs -n 1`, `timeout 150`, `FOO=1`, `npx`,
# `./node_modules/.bin/`), and the openings of blocks (`if ($?) {`, `then`, `( ... )`).
_PREFIXES = tuple(re.compile(p, re.I) for p in (
    r"^(?:if|elseif|while|foreach|for)\s*\((?:[^()]|\([^()]*\))*\)\s*\{\s*",
    r"^(?:if|elif|while|until)\s+(?=[^\s(])",
    r"^(?:then|do|else|try|finally)\b\s*\{?\s*",
    r"^[{(]\s*",
    r"^&\s*",
    r"^(?:\w+=\S*\s+)+",
    r"^(?:sudo|doas)(?:\s+(?:-[ugCphUrtD]\s+\S+|-\S+))*\s+",
    r"^xargs(?:\s+(?:-[nIPLdsaE]\s+\S+|-\S+))*\s+",
    r"^env(?:\s+(?:-[uCS]\s+\S+|-\S+))*\s+",
    r"^(?:nohup|exec|command)(?:\s+-\S+)*\s+",
    r"^(?:timeout(?:\s+(?:-[sk]\s+\S+|-\S+))*\s+\d+[smhd]?|time|nice(?:\s+-n\s+\S+|\s+-\S+)*|npx|bunx"
    r"|pnpm\s+(?:exec|dlx)|yarn\s+dlx|uv\s+run|poetry\s+run|pipenv\s+run|pdm\s+run|hatch\s+run|bundle\s+exec)\s+",
    r"^[\"']?[^\s\"']*[\\/](?=[\w.-]+[\"']?(?:\s|$))",
))
_VAR_PYTHON = re.compile(r"^[\"']?\$\{?\w+\}?[\"']?(?=\s+-m\s)")
_REDIRECT = re.compile(r"\s+\d?>>?&?\s*\S+")


def _split(text: str, pipes=True, ands=True, ors=True) -> list[str]:
    """A command line cut at && || ; new lines and (with `pipes`) single |, but not inside quotes. A quote that's
    never closed (`echo don't`) can't be told from an apostrophe, so then it's cut as if there were none."""
    out, cur, quote, i, n = [], [], None, 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and nxt:
                cur.append(nxt)
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur.append(c)
        elif c == "\\" and nxt:
            cur.append(c + nxt)
            i += 1
        elif (c == "&" and nxt == "&" and not ands) or (c == "|" and nxt == "|" and not ors):
            cur.append(c + nxt)
            i += 1
        elif c in "&|" and nxt == c:
            out.append("".join(cur))
            cur = []
            i += 1
        elif c in "\n;" or (pipes and c == "|"):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if quote:
        return re.split(r"\n|&&|\|\||[;|]" if pipes else r"\n|&&|\|\||;", text)
    out.append("".join(cur))
    return out


def _script(cmd) -> str:
    """A command line as the shell would run it: heredoc bodies (text written into a file) and code in another language
    (node -e "...", python -c "...") left out, a shell's own script (bash -c "...", cmd /c ...) kept as commands."""
    def unquote(q):
        return re.sub(r"\\([\"\\])", r"\1", q[1:-1])

    s = str(cmd or "")
    s = re.sub(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1[^\n]*\n[\s\S]*?\n[ \t]*\2[ \t]*(?=\n|\Z)", "", s)
    s = re.sub(r"\b(?:bash|sh|zsh|dash|fish|pwsh|powershell)(?:\.exe)?\b[^\"'\n;&|]*?\s-(?:c|Command)\s+"
               r"(\"(?:[^\"\\]|\\.)*\"|'[^']*')", lambda m: f";{unquote(m[1])};", s, flags=re.I)
    s = re.sub(r"\bcmd(?:\.exe)?\s+/[ck]\s+", ";", s, flags=re.I)
    s = re.sub(r"\b(?:node|deno|bun|python3?|py|ruby|perl|php)(?:\.exe)?\b[^\"'\n;&|]*?\s-(?:e|c|p|r|-eval)\s+"
               r"(\"(?:[^\"\\]|\\.)*\"|'[^']*')", lambda m: m[0][:len(m[0]) - len(m[1])] + '""', s, flags=re.I)
    return re.sub(r"\s-exec(?:dir)?\s+", ";", s)   # find ... -exec rm -rf {} \;


def _parts(cmd) -> list[str]:
    """The commands a command line actually runs, each from its start, wrappers and prefixes looked through. Text
    written into a file and code in another language are left out, so writing a test that mentions `git push` or
    `rm -rf` doesn't count as doing it."""
    out = []
    for part in _split(_script(cmd)):
        s = part.strip()
        for _ in range(6):
            for rx in _PREFIXES:
                s = rx.sub("", s, count=1)
        s = _VAR_PYTHON.sub("python", s)        # "$PY" -m pytest: a Python kept in a variable
        if s:
            out.append(s)
    return out


def _runs_tests(text: str) -> bool:
    return any(_TEST.match(p) for p in _parts(text))


def is_test(cmd: str) -> bool:
    """The command runs tests: a test command at the start of one of its parts ("cd app && pytest", "CI=1 npm test |
    tail"), not just mentioned in it (a script being written, an echo, a grep for "npm test")."""
    return _runs_tests(cmd)


def is_check(cmd: str) -> bool:
    """The command runs a linter or type checker (ruff, eslint, tsc, mypy, npm run lint ...)."""
    return any(_CHECK.match(p) for p in _parts(cmd))


def ships(cmd: str) -> bool:
    """It commits, pushes, opens a pull request or publishes. Not when it only does that after its own tests pass
    (`npm test && git commit`); it does when the tests can't stop it (`npm test; git commit`, `npm test || git
    push`)."""
    for statement in _split(_script(cmd), pipes=False, ands=False):
        # A && chain: each step runs only if the one before it worked. With an || in it, no such promise.
        chain = [statement] if "||" in statement else _split(statement, pipes=False)
        for i, link in enumerate(chain):
            if any(_SHIPS.match(p) for p in _parts(link)) and not any(_runs_tests(c) for c in chain[:i]):
                return True
    return False


def _pipe(cmd) -> str:
    """"| tail" when a test (or check) command's output goes through a pipe, else ""."""
    for statement in _split(_script(cmd), pipes=False):
        stages = _split(statement)
        if len(stages) > 1 and any(_TEST.match(p) or _CHECK.match(p) for p in _parts(";".join(stages[:-1]))):
            last = _parts(stages[-1])
            return f"| {last[0].split()[0]}" if last else "|"
    return ""


def piped(cmd: str) -> bool:
    """The tests' output goes through a pipe (`| tail`, `| grep`, `| tee`): the shell then reports the last command's
    exit code, not the tests', and a filter may have cut the summary."""
    return bool(_pipe(cmd))


def _exit_not_tests(cmd) -> bool:
    """Another command runs after the tests whatever they did (`npm test > out.txt; echo done`): the exit code is then
    the last command's. After && or || it isn't "whatever they did"."""
    statements = [s.strip() for s in _split(_script(cmd), pipes=False, ands=False, ors=False) if s.strip()]
    runs = [i for i, s in enumerate(statements) if _runs_tests(s)]
    return bool(runs) and runs[-1] < len(statements) - 1


def _key(cmd) -> str:
    """What a command runs, for spotting the next try at it: the test command itself (`node --test x.js 2>&1 | grep
    fail` and `... | tail` are one test run twice), else the command without trailing filters and redirections."""
    test = next((p for p in _parts(cmd) if _TEST.match(p)), None)
    if test:
        return " ".join(_REDIRECT.sub("", test).split())
    out = []
    for statement in _split(_script(cmd), pipes=False):
        stages = [s.strip() for s in _split(statement)]
        while len(stages) > 1 and _FILTER.match(stages[-1]):
            stages.pop()
        out.append(" | ".join(stages))
    return " ".join(_REDIRECT.sub("", " ; ".join(s for s in out if s)).split())


_FILTER = re.compile(r"(?:tail|head|grep|egrep|rg|less|more|cat|tee|sed|awk|sort|uniq|wc|cut|tr|column|jq"
                     r"|Select-String|Select-Object|findstr|Out-String)\b", re.I)
_CUTTERS = {"tail", "head", "grep", "egrep", "rg", "sed", "awk", "cut", "select-string", "select-object", "findstr",
            "jq", "less", "more", "uniq", "sort"}


def same_command(a: str, b: str) -> bool:
    """Two runs of the same command, give or take whitespace, `2>&1` and a trailing `| tail` / `| grep`."""
    ka, kb = _key(a), _key(b)
    return bool(ka) and ka == kb


def whole_suite(cmd: str) -> bool:
    """A test run with no file, folder or name filter after the runner (`npm test`, `pytest -q`, `go test ./...`):
    its pass also fixes the failures of an earlier, narrower run."""
    for p in _parts(cmd):
        if not _TEST.match(p) or re.match(r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test:", p):
            continue
        words = [w for w in _REDIRECT.sub("", p).split()[1:] if w not in ("./...", ".")]
        if not any(re.search(r"[\\/]|\.(?:[cm]?[jt]sx?|py|go|rs|rb|php|java|cs|swift)$", w, re.I)
                   or re.match(r"(?:-k|-t|--grep|--test-name-pattern|--testNamePattern|--filter|-run)(?:=|$)", w)
                   for w in words):
            return True
    return False


# -- how a test run ended ---------------------------------------------------------------------------------------------

# Output that looks like something broke, or like everything went fine, when there's no summary to count from.
_BROKE = re.compile(r"\bFAIL(?:ED|URE)?\b|\bfailing\b|^Traceback \(most recent call last\)|\bpanicked\b|\b\w*Error:"
                    r"|^\s*[✕✖×]\s|^not ok\b", re.M)
_FINE = re.compile(r"^\s*(?:PASS|ok)\s|\ball (?:\d+ )?tests? passed\b|^OK\b|^\s*[✓✔]\s", re.M)


def _count_line(f: dict) -> str:
    """"48 passed", "48 passed, 2 skipped", "2 of 48 failed", "1 error"."""
    if f["failed"] or f["errors"]:
        bits = []
        if f["failed"]:
            some = f["passed"] or f["skipped"]
            bits.append(f"{f['failed']} of {f['total']} failed" if some else f"{f['failed']} failed")
        if f["errors"]:
            bits.append(_plural(f["errors"], "error"))
        return ", ".join(bits)
    return f"{f['passed']} passed" + (f", {f['skipped']} skipped" if f["skipped"] else "")


def verdict(cmd: str, output: str, exit_code: int | None, interrupted: bool = False) -> dict:
    """How a test (or check) run ended, and how we know:
        {"state": "passed" | "failed" | "unclear", "line", "passed", "failed", "skipped", "errors", "total",
         "framework", "failing", "reason", "source": "summary" | "exit code"}
    The output's own summary decides when there is one: a command can fail after its tests passed (`npm test &&
    restart`), and the tests are what count. Zero tests, or only skipped ones, is unclear: a run where nothing ran
    isn't a pass. With no summary the exit code decides, unless the run was interrupted, the exit code is unknown
    (None), the output went through a pipe, another command ran after the tests, or the output disagrees with the
    exit code (exit 0 with a Traceback in it; exit 1 with only passes). Unclear is never a pass."""
    lines = _lines(output)
    f = _parse(lines)
    v = {"state": "unclear", "line": "", "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "total": 0,
         "framework": "", "failing": [], "reason": "", "source": "exit code"}
    if f:
        v.update({k: f[k] for k in ("passed", "failed", "skipped", "errors", "total", "failing")},
                 framework=f["runner"], source="summary")
        if f["failed"] + f["errors"]:
            v.update(state="failed", line=_count_line(f))
        elif f["passed"]:
            v.update(state="passed", line=_count_line(f))
        elif f["lint"] and is_check(cmd) and not is_test(cmd):
            v.update(state="passed", line="no problems")
        else:
            v["line"] = "no tests actually ran" + (f" ({f['skipped']} skipped)" if f["skipped"] else "")
    elif interrupted:
        v["line"] = "it didn't finish"
    elif exit_code is None:
        v["line"] = "exit code unknown"
    elif pipe := _pipe(cmd):
        cut = pipe.split()[-1].lower() in _CUTTERS
        v["line"] = f"output was cut ({pipe})" if cut else f"output went through a pipe ({pipe})"
    elif _exit_not_tests(cmd):
        v["line"] = "another command ran after the tests"
    else:
        text = "\n".join(lines)
        if exit_code != 0:
            if _FINE.search(text) and not _BROKE.search(text):
                v["line"] = f"exit code {exit_code}, but the output looks fine"
            else:
                v.update(state="failed", line=f"failed (exit code {exit_code})")
        elif _BROKE.search(text):
            v["line"] = "exit code 0, but the output shows errors"
        else:
            v.update(state="passed", line="passed (exit code only)")
    if v["state"] != "passed":
        v["reason"] = _reason_text(lines)
    return v


def only_old_failures(before: dict | None, now: dict) -> bool:
    """A failed run (a verdict) fails only tests that were already failing before the agent changed anything.
    `before`: the verdict of its last test run before its first change, or None. An agent is held to the failures it
    caused, not the ones it found."""
    if not before or before.get("state") != "failed" or now.get("state") != "failed":
        return False
    old, names = set(before.get("failing") or ()), now.get("failing") or []
    return bool(old) and bool(names) and all(n in old for n in names)


# -- worth a second look ----------------------------------------------------------------------------------------------

# Files that usually hold secrets (not the .env.example templates next to them).
SECRET = re.compile(
    r"(?:^|[\\/])(?:\.env(?!\.(?:example|sample|template|dist|defaults?)$)(?:\.[\w-]+)?|\.npmrc|\.pypirc|\.netrc"
    r"|\.git-credentials|id_(?:rsa|ed25519|ecdsa|dsa)|credentials?(?:\.json)?|secrets?\.\w+"
    r"|[\w-]*\.(?:pem|key|p12|pfx))$",
    re.I)

_RM = re.compile(r"\brm\s+(?:-{1,2}\S+\s+)*(?:-[a-z]*r[a-z]*|--recursive)\b|\bRemove-Item\b[^\n]*-Recurse"
                 r"|\brmdir\s+/s\b|\bdel\s+/[sq]", re.I)
# (pattern, flag (text, or made from the match), where): "start" = a command it ran starts with it; "script" = anywhere
# in what the shell runs (SQL inside `psql -c "..."`, a pipe into a shell); "unquoted" = the same minus quoted text
# (a "sudo" in a grep pattern isn't one being run); "raw" = the command line as written.
_RULES = (
    (_RM, "rm -rf", "start"),
    (re.compile(r"\bgit\s+push\b[^\n]*(?:--force\b|\s-f\b|\s\+\S)", re.I), "force-push", "start"),
    (re.compile(r"\bgit\s+reset\b[^\n|;&]*\s--hard\b", re.I), "git reset --hard", "start"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.I), "git clean -f", "start"),
    (re.compile(r"\bgit\s+checkout\s+--\s", re.I), "git checkout --", "start"),
    (re.compile(r"\bgit\s+restore\s+\.", re.I), "git restore .", "start"),
    (re.compile(r"\b(?:drop\s+(?:table|database)|truncate\s+table)\b", re.I),
     lambda m: " ".join(m[0].upper().split()), "script"),
    (re.compile(r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n|]*\|\s*(?:sudo\s+)?"
                r"(?:sh|bash|zsh|iex|Invoke-Expression)\b", re.I),
     lambda m: f"{m[0].split()[0].lower()} | {m[0].split()[-1].lower()}", "script"),
    (re.compile(r"\b(?:sh|bash|zsh)\b[^\n|;&]*?(?:\$\(|<\()\s*(?:curl|wget)\b", re.I), "curl | sh", "raw"),
    (re.compile(r"\b(?:sudo|doas)\b", re.I), "sudo", "unquoted"),
    (re.compile(r"\bchmod\s+(?:-R\s+)?(?:0?777|a\+rwx)\b", re.I), "chmod 777", "unquoted"),
    (re.compile(r"\b(?:taskkill|Stop-Process|kill\s+-(?:9|KILL|SIGKILL)|pkill|killall)\b", re.I),
     lambda m: " ".join(m[0].split()), "unquoted"),
)
RISKY = re.compile("|".join(f"(?:{rx.pattern})" for rx, _, _ in _RULES), re.I)

# A path that's fine to delete: temp and scratch space, build output, installed packages.
_THROWAWAY = re.compile(
    r"\$TEMP|%TEMP%|\$env:TEMP|\$\{?TMPDIR|(?:^|[\\/])te?mp(?:[\\/]|$)|AppData[\\/]Local[\\/]Temp"
    r"|(?:^|[\\/])var[\\/]folders[\\/]"
    r"|(?:^|[\\/])(?:scratchpad|node_modules|dist|build|out|coverage|\.next|\.cache|__pycache__|\.pytest_cache"
    r"|\.mypy_cache|\.ruff_cache|target|DerivedData)(?:[\\/]|$)",
    re.I)
_FILE_CHANGES = {"edit", "write", "multiedit", "create", "delete", "patch", "apply", "applypatch", "notebookedit",
                 "update", "remove", "move", "rename", "save"}
_RUN_VERBS = {"run", "bash", "shell", "exec", "command", "terminal"}


def _shell_vars(cmd: str) -> dict:
    """Variables a command sets, for the paths a delete names: T=$(mktemp -d) is a temp folder."""
    out = {}
    for m in re.finditer(r"(?:^|[\s;&|(])(?:export\s+|local\s+)?([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|\$\([^)]*\)"
                         r"|[^\s;&|]+)", cmd):
        value = re.sub(r"^[\"']|[\"']$", "", m[2])
        out[m[1]] = "$TEMP/mktemp" if re.match(r"\$\(\s*mktemp\b", value) else value
    return out


def _throwaway(part: str, names: dict) -> bool:
    """A delete whose every target is temp, scratch or build space: housekeeping, not a risk."""
    targets = []
    for t in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", part)[1:]:
        if t.startswith("-") or re.fullmatch(r"/[a-z]", t, re.I):
            continue
        t = re.sub(r"^[\"']|[\"']$", "", t)
        targets.append(re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: names.get(m[1], m[0]), t))
    return bool(targets) and all(_THROWAWAY.search(t) for t in targets)


def _command_risk(cmd: str) -> str:
    script = _script(cmd)
    if not (RISKY.search(cmd) or RISKY.search(script)):
        return ""
    ran = _parts(cmd)
    unquoted = re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", '""', script)
    for rx, flag, where in _RULES:
        if where == "start":
            hits = [p for p in ran if rx.match(p)]
            if not hits or (rx is _RM and all(_throwaway(p, _shell_vars(cmd)) for p in hits)):
                continue
            m = rx.match(hits[0])
        else:
            m = rx.search({"script": script, "unquoted": unquoted, "raw": cmd}[where])
            if not m:
                continue
        return flag(m) if callable(flag) else flag
    return ""


def risk(verb: str, target: str, cmd: str = "", path: str = "") -> str:
    """A short flag for a step worth a second look, or "": "changed .env" / "read .env" (a file that usually holds
    secrets), "rm -rf", "force-push", "git reset --hard", "DROP TABLE", "curl | sh", "sudo", "chmod 777", "kill -9" ...
    `verb` is the step's (Read / Edit / Write / Run ...), `target` its file or command."""
    v = (verb or "").strip().lower()
    run = v in _RUN_VERBS
    file = str(path or ("" if run else target) or "").strip()
    if file and SECRET.search(file.rstrip("/\\")):
        name = re.split(r"[\\/]", file.rstrip("/\\"))[-1]
        return f"{'changed' if v in _FILE_CHANGES else 'read'} {name}"
    command = cmd or (target if run else "")
    return _command_risk(str(command)) if command else ""


# -- tests weakened to pass -------------------------------------------------------------------------------------------

# A test file: in a test folder, or named like one (cart.test.js, cart.spec.ts, test_cart.py, cart_test.go,
# cart_spec.rb, CartTests.swift, CartTest.java).
_TEST_FILE = re.compile(r"(?:^|[\\/])(?:tests?|__tests__|spec)[\\/]|\.(?:test|spec)\.\w+$|(?:^|[\\/])test_[^\\/]*\.py$"
                        r"|_test\.(?:go|py)$|_spec\.rb$", re.I)
_TEST_CLASS_FILE = re.compile(r"(?:^|[\\/])[A-Z]\w*Tests?\.(?:swift|java|kt|cs)$")
# A line that checks something, in the usual test libraries.
_ASSERTION = re.compile(r"\b(?:assert\w*|expect)!?\s*[.(]|^assert\s|\bt\.(?:equal|deepEqual|is|ok|true|false|same)\s*\("
                        r"|\bself\.assert\w+\s*\(|\.should\b|\bXCTAssert\w*\s*\(|\brequire\.\w+\s*\("
                        r"|\bt\.(?:Error|Errorf|Fatal|Fatalf)\s*\(")
# Turning tests off: skip, todo or only (the others stop running), xit, pytest's and unittest's skips, go's t.Skip,
# rust's #[ignore], XCTSkip.
_SKIPS = re.compile(r"\b(?:it|test|describe|context)\.(?:skip|todo|only)\b|\bx(?:it|test|describe)\s*\("
                    r"|@pytest\.mark\.(?:skip|xfail)|\bpytest\.skip\s*\(|@unittest\.skip|\bself\.skipTest\s*\("
                    r"|\bt\.Skip(?:Now|f)?\(|#\[ignore\]|\bskip:\s*true\b|\bXCTSkip\w*\s*\(")
_COMMENT = re.compile(r"(?://|#(?!\[)|/\*|\*|--)")
# The values in a line: strings, numbers, true/false/null.
_VALUES = re.compile(r"([\"'`])(?:\\.|(?!\1).)*\1|-?\b\d[\d_.]*\b"
                     r"|\b(?:true|false|null|undefined|None|True|False|nil)\b")


def is_test_file(path: str) -> bool:
    p = str(path or "")
    return bool(_TEST_FILE.search(p) or _TEST_CLASS_FILE.search(p))


def weakened(lines: list[tuple[str, int | None, str]], path: str) -> str:
    """Why an edit to a test file looks like a pass faked by changing the test, or "": "removed an assertion" (taken
    out or commented out), "added a skip" (skip, only, todo, xit ...), "changed what a test expects" (the same check
    with a different value). `lines`: the edit's diff, (sign "+" / "-" / " ", line number, text). Lines the edit only
    moved or re-spaced don't count; neither does a new test. Whether the code was fixed too, or the agent is only
    correcting a test it just wrote, is the caller's to weigh."""
    if not is_test_file(path):
        return ""
    gone, added = [], []
    for sign, _, text in lines:
        line = " ".join(str(text).split())
        if line and sign == "-":
            gone.append(line)
        elif line and sign == "+":
            added.append(line)
    for i in range(len(gone) - 1, -1, -1):
        if gone[i] in added:
            added.remove(gone[i])
            del gone[i]
    lost = [ln for ln in gone if not _COMMENT.match(ln) and _ASSERTION.search(ln)]
    kept = [ln for ln in added if not _COMMENT.match(ln) and _ASSERTION.search(ln)]
    shapes = {_VALUES.sub("#", ln) for ln in kept}
    changed = sum(1 for ln in lost if _VALUES.sub("#", ln) in shapes)
    removed = max(0, len(lost) - len(kept))
    skipped = sum(1 for ln in added if not _COMMENT.match(ln) and _SKIPS.search(ln))
    what = []
    if removed:
        what.append("removed an assertion" if removed == 1 else f"removed {removed} assertions")
    if skipped:
        what.append("added a skip" if skipped == 1 else f"added {skipped} skips")
    if changed:
        what.append("changed what a test expects" if changed == 1 else f"changed what {changed} assertions expect")
    return ", ".join(what)
