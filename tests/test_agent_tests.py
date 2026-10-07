"""Coding agents' test runs read from their logs: the runners' outputs, why a test failed, when a run is unclear, risky
steps and tests weakened to pass. Many cases are ported from dotpals' test/testout.test.js and test/story.test.js."""
import pytest

try:
    from mint.tools import agent_tests as at
except ImportError:
    from mint import agent_tests as at


def counts(out):
    f = at.parse(out)
    return [f["framework"], f["passed"], f["failed"], f["errors"], f["skipped"]]


# -- realistic outputs ------------------------------------------------------------------------------------------------

PYTEST_PASS = """\
============================= test session starts ==============================
platform darwin -- Python 3.12.4, pytest-8.3.2, pluggy-1.5.0
rootdir: /home/dev/app
collected 48 items

tests/test_api.py ........................                               [ 50%]
tests/test_models.py ........................                            [100%]

============================== 48 passed in 1.23s ==============================
"""

PYTEST_FAIL = """\
collected 3 items

tests/test_math.py .F.                                                   [100%]

=================================== FAILURES ===================================
__________________________________ test_total __________________________________

    def test_total():
>       assert total([1, 2]) == 3
E       assert -1 == 3
E        +  where -1 = total([1, 2])

tests/test_math.py:7: AssertionError
=========================== short test summary info ============================
FAILED tests/test_math.py::test_total - assert -1 == 3
========================= 1 failed, 2 passed in 0.05s ==========================
"""

PYTEST_COLLECT_ERROR = """\
collected 0 items / 1 error

==================================== ERRORS ====================================
____________________ ERROR collecting tests/test_api.py _____________________
ImportError while importing test module '/home/dev/app/tests/test_api.py'.
Traceback:
tests/test_api.py:3: in <module>
    from app.api import create_app
E   ModuleNotFoundError: No module named 'app.api'
=========================== short test summary info ============================
ERROR tests/test_api.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.12s ===============================
"""

JEST = """\
FAIL src/cart.test.js
  ● cart › adds items

    expect(received).toBe(expected) // Object.is equality

    Expected: 3
    Received: -1

      10 |   const cart = new Cart();
      11 |   cart.add(1); cart.add(2);
    > 12 |   expect(cart.count).toBe(3);
         |                      ^

      at Object.<anonymous> (src/cart.test.js:12:24)

PASS src/math.test.js

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 11 passed, 12 total
Snapshots:   0 total
Time:        0.82 s
Ran all test suites.
"""

VITEST = """\
 ✓ src/utils.test.ts (5 tests) 3ms
 ❯ src/cart.test.ts (3 tests | 1 failed) 6ms
   × cart > adds items 4ms
     → expected -1 to be 3 // Object.is equality

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/cart.test.ts > cart > adds items
AssertionError: expected -1 to be 3 // Object.is equality
 ❯ src/cart.test.ts:9:22

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 7 passed (8)
   Start at  10:12:01
   Duration  412ms
"""

GO_VERBOSE = """\
=== RUN   TestSum
    math_test.go:8: sum(1, 2) = -1; want 3
--- FAIL: TestSum (0.00s)
=== RUN   TestDiv
--- PASS: TestDiv (0.00s)
FAIL
FAIL\texample.com/calc\t0.004s
ok  \texample.com/util\t0.002s
FAIL
"""

CARGO = """\
   Compiling calc v0.1.0 (/home/dev/calc)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.52s
     Running unittests src/lib.rs (target/debug/deps/calc-1a2b3c)

running 3 tests
test tests::adds ... ok
test tests::divides ... FAILED
test tests::subtracts ... ok

failures:

---- tests::divides stdout ----
thread 'tests::divides' panicked at src/lib.rs:21:9:
assertion `left == right` failed
  left: 2
 right: 3
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    tests::divides

test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

error: test failed, to rerun pass `--lib`
"""

NODE_TEST = """\
▶ math
  ✔ adds (0.6ms)
  ✖ subtracts (1.1ms)
    AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:

    -1 !== 3

        at TestContext.<anonymous> (file:///home/dev/app/test/math.test.js:9:12)
        at async Test.run (node:internal/test_runner/test:935:9) {
      generatedMessage: true,
      code: 'ERR_ASSERTION',
      actual: -1,
      expected: 3,
      operator: 'strictEqual'
    }

✖ math (2.4ms)
ℹ tests 2
ℹ suites 1
ℹ pass 1
ℹ fail 1
ℹ cancelled 0
ℹ skipped 0
ℹ todo 0
ℹ duration_ms 52.1
"""

MOCHA = """\
  Array
    #indexOf()
      ✔ returns -1 when the value is missing
      1) returns the index


  1 passing (6ms)
  1 failing

  1) Array
       #indexOf()
         returns the index:

      AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:

-1 !== 0

      at Context.<anonymous> (test/array.spec.js:8:14)
"""

UNITTEST = """\
..F.
======================================================================
FAIL: test_total (tests.test_cart.CartTest.test_total)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/dev/app/tests/test_cart.py", line 12, in test_total
    self.assertEqual(cart.total(), 30)
AssertionError: 25 != 30

----------------------------------------------------------------------
Ran 4 tests in 0.003s

FAILED (failures=1)
"""


def test_realistic_outputs():
    assert counts(PYTEST_PASS) == ["pytest", 48, 0, 0, 0]
    assert counts(PYTEST_FAIL) == ["pytest", 2, 1, 0, 0]
    assert at.parse(PYTEST_FAIL)["failing"] == ["tests/test_math.py::test_total"]
    assert counts("=============== 4 skipped in 0.02s ===============") == ["pytest", 0, 0, 0, 4]
    assert counts(PYTEST_COLLECT_ERROR) == ["pytest", 0, 0, 1, 0]
    assert counts(JEST) == ["jest", 11, 1, 0, 0]
    assert at.parse(JEST)["failing"] == ["cart › adds items"]
    assert counts(VITEST) == ["vitest", 7, 1, 0, 0]
    assert counts(GO_VERBOSE) == ["go test", 1, 1, 0, 0]
    assert counts(CARGO) == ["cargo test", 2, 1, 0, 0]
    assert at.parse(CARGO)["failing"] == ["tests::divides"]
    assert counts(NODE_TEST) == ["node:test", 1, 1, 0, 0]
    assert at.parse(NODE_TEST)["failing"] == ["math"]   # top-level ids only, as dotpals (nested ones are indented)
    assert counts(MOCHA) == ["mocha", 1, 1, 0, 0]
    assert counts(UNITTEST) == ["unittest", 3, 1, 0, 0]
    assert at.parse(PYTEST_PASS)["total"] == 48
    assert set(at.parse(PYTEST_PASS)) == {"framework", "passed", "failed", "skipped", "total", "errors", "failing"}


def test_failure_reasons_from_realistic_outputs():
    assert at.failure_reason(PYTEST_FAIL) == "assert -1 == 3 (tests/test_math.py:7)"
    assert at.failure_reason(PYTEST_COLLECT_ERROR) == "ModuleNotFoundError: No module named 'app.api' (tests/test_api.py:3)"
    assert at.failure_reason(JEST) == "expected 3, got -1 (src/cart.test.js:12)"
    assert at.failure_reason(VITEST) == "AssertionError: expected -1 to be 3 // Object.is equality (src/cart.test.ts:9)"
    # go test -v logs the reason before "--- FAIL".
    assert at.failure_reason(GO_VERBOSE) == "sum(1, 2) = -1; want 3 (math_test.go:8)"
    assert at.failure_reason(CARGO) == "assertion `left == right` failed (src/lib.rs:21)"
    assert at.failure_reason(NODE_TEST) == "expected 3, got -1 (test/math.test.js:9)"
    assert at.failure_reason(MOCHA) == (
        "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal: -1 !== 0 (test/array.spec.js:8)")
    assert at.failure_reason(UNITTEST) == "AssertionError: 25 != 30 (tests/test_cart.py:12)"
    assert at.failure_reason(PYTEST_PASS) == ""
    assert len(at.failure_reason("AssertionError: " + "x" * 400 + "\n at t (test/a.test.js:3:1)")) <= 140


# -- ported from dotpals' testout.test.js -----------------------------------------------------------------------------

def test_javascript_runners():
    assert counts("Test Suites: 1 failed, 3 passed, 4 total\nTests:       1 failed, 47 passed, 48 total\n"
                  "  ● math › adds\n") == ["jest", 47, 1, 0, 0]
    assert at.parse("  ● math › adds\nTests: 1 failed, 2 passed, 3 total")["failing"] == ["math › adds"]
    assert counts(" ✓ src/a.test.ts (3 tests)\n Test Files  1 passed (1)\n      Tests  3 passed | 1 skipped (4)\n") == \
        ["vitest", 3, 0, 0, 1]
    assert counts("  12 passing (40ms)\n  2 pending\n  1 failing\n\n  1) Array\n       #indexOf():\n     Error: boom") \
        == ["mocha", 12, 1, 0, 2]
    assert counts("✔ adds (0.5ms)\nℹ tests 104\nℹ pass 104\nℹ fail 0\nℹ skipped 0") == ["node:test", 104, 0, 0, 0]
    assert counts("not ok 3 - parses dates\n# tests 5\n# pass 4\n# fail 1") == ["node:test", 4, 1, 0, 0]
    # A filtered summary ("npm test | grep pass") still counts.
    assert counts("Exit code 255\nℹ pass 102\nℹ fail 0") == ["node:test", 102, 0, 0, 0]


def test_python_go_rust_dotnet_java_php_ruby_deno_bun():
    out = "tests/test_a.py ..F\nFAILED tests/test_a.py::test_div - ZeroDivisionError\n===== 1 failed, 2 passed in 0.31s ====="
    assert counts(out) == ["pytest", 2, 1, 0, 0]
    assert counts("..s\n" + "-" * 70 + "\nRan 3 tests in 0.002s\n\nOK (skipped=1)") == ["unittest", 2, 0, 0, 1]
    assert counts("FAIL: test_x (tests.T)\nRan 4 tests in 0.01s\n\nFAILED (failures=1)") == ["unittest", 3, 1, 0, 0]
    assert counts("--- FAIL: TestDiv (0.00s)\nFAIL\nFAIL\texample.com/m\t0.01s\nok  \texample.com/other\t0.02s") == \
        ["go test", 1, 1, 0, 0]
    assert counts("test a ... ok\ntest b ... FAILED\ntest result: FAILED. 3 passed; 1 failed; 0 ignored; 0 measured") \
        == ["cargo test", 3, 1, 0, 0]
    assert counts("Passed!  - Failed:     0, Passed:    12, Skipped:     1, Total:    13, Duration: 1 s") == \
        ["dotnet test", 12, 0, 0, 1]
    assert counts("[INFO] Tests run: 9, Failures: 1, Errors: 0, Skipped: 2") == ["maven", 6, 1, 0, 2]
    assert counts("MathTest > adds() FAILED\n5 tests completed, 1 failed, 1 skipped") == ["gradle", 3, 1, 0, 1]
    assert counts("There was 1 failure:\n\n1) App\\MathTest::testAdd\nFAILURES!\nTests: 5, Assertions: 9, Failures: 1.") \
        == ["phpunit", 4, 1, 0, 0]
    assert counts("Finished in 0.1 seconds\n10 examples, 2 failures, 1 pending\n\nrspec ./spec/a_spec.rb:4 # A works") \
        == ["rspec", 7, 2, 0, 1]
    assert counts("ok | 5 passed | 0 failed | 1 ignored (12ms)") == ["deno", 5, 0, 0, 1]
    assert counts(" 7 pass\n 1 fail\n 2 skip\nRan 10 tests across 2 files.") == ["bun", 7, 1, 0, 2]


def test_zero_tests_only_skipped_and_nothing_to_read():
    none = at.parse("============ no tests ran in 0.01s ============")
    assert none and none["total"] == 0
    jest_none = at.parse("No tests found, exiting with code 1")
    assert jest_none and jest_none["passed"] == 0
    skipped = at.parse("ℹ tests 3\nℹ pass 0\nℹ fail 0\nℹ skipped 3")
    assert [skipped["passed"], skipped["skipped"]] == [0, 3]
    assert at.parse("Compiling…\nDone.") is None
    assert at.parse("") is None
    assert at.parse(None) is None


def test_worst_case_wins_and_linters_add_errors():
    # A passing summary doesn't hide a failure marker printed earlier.
    assert at.parse("  ● suite › breaks\nTests:       0 failed, 5 passed, 5 total")["failed"] == 1
    both = at.parse("src/a.ts(3,5): error TS2322: Type mismatch.\nFound 1 error.\nℹ tests 4\nℹ pass 4\nℹ fail 0")
    assert both["passed"] == 4 and both["errors"] == 1 and "node:test" in both["framework"]
    # Colors don't get in the way.
    assert at.parse("\x1b[32m===== 3 passed in 0.1s =====\x1b[0m")["passed"] == 3
    # A last resort for runners without their own parser (Playwright).
    assert counts("  1 failed\n    [chromium] › a.spec.ts:3:1 › works\n  4 passed (3.0s)") == ["tests", 4, 1, 0, 0]
    # Linters alone: their errors, no tests.
    assert counts("src/a.py:1:8: F401 [*] `os` imported but unused\nFound 1 error.\n[*] 1 fixable with the `--fix` option.") \
        == ["ruff", 0, 0, 1, 0]
    assert counts("/app/src/a.js\n  3:7  error  'x' is never reassigned  prefer-const\n\n✖ 1 problem (1 error, 0 warnings)") \
        == ["eslint", 0, 0, 1, 0]


def test_failure_reason_in_each_runners_words():
    node = ("✖ sum adds two numbers (1.2ms)\n  AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n"
            "  -1 !== 3\n\n      at TestContext.<anonymous> (file:///C:/w/app/test/math.test.js:5:10)\n"
            "      at Test.runInAsyncScope (node:internal/test_runner/test:1004:9) {\n    actual: -1,\n    expected: 3,\n  }")
    assert at._why(at._lines(node)) == ("expected 3, got -1", "test/math.test.js:5")
    assert at.failure_reason("  ● cart › adds items\n\n    Expected: 3\n    Received: -1\n\n"
                             "      at Object.<anonymous> (src/cart.test.js:12:20)") == "expected 3, got -1 (src/cart.test.js:12)"
    assert at.failure_reason(">       assert sum(1, 2) == 3\nE       assert -1 == 3\nE        +  where -1 = sum(1, 2)\n\n"
                             "tests/test_math.py:4: AssertionError") == "assert -1 == 3 (tests/test_math.py:4)"
    assert at.failure_reason("--- FAIL: TestSum (0.00s)\n    math_test.go:8: sum(1, 2) = -1; want 3\nFAIL") == \
        "sum(1, 2) = -1; want 3 (math_test.go:8)"
    assert at.failure_reason("     AssertionError: expected -1 to equal 3\n      at Context.<anonymous> (test/sum.spec.js:7:24)") \
        == "AssertionError: expected -1 to equal 3 (test/sum.spec.js:7)"
    # No values to compare: the assertion's own words, and what follows its colon.
    assert at.failure_reason("AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n-1 !== 3") == \
        "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal: -1 !== 3"
    # "Must not match": not "expected X, got Y" (that reads backwards), but the assertion's own words.
    negated = ("✖ redacts\n  AssertionError [ERR_ASSERTION]: The input was expected to not match the regular expression "
               "/secret/. Input:\n    actual: 'token=secret',\n    expected: /secret/,")
    assert at.failure_reason(negated)[:80] == \
        "AssertionError [ERR_ASSERTION]: The input was expected to not match the regular "
    # Another command crashed before the tests ran (`node -e ...; npm test`): that isn't why the tests failed.
    assert at.failure_reason("SyntaxError: Invalid hexadecimal escape sequence\n    at node:internal/main/eval_string:37:3\n"
                             "✖ findConflict: own session (7.4ms)\nℹ fail 1") == ""
    # A test file that wouldn't load: its error, printed just before it, is the reason.
    assert at.failure_reason("SyntaxError: Unexpected token '.'\n✖ test\\tracing.test.js (77.2ms)") == \
        "SyntaxError: Unexpected token '.'"
    # Through `grep -n`: line numbers in front.
    assert at.failure_reason("160-  AssertionError [ERR_ASSERTION]: Expected values to be strictly deep-equal:\n"
                             "161-  + actual - expected") == \
        "AssertionError [ERR_ASSERTION]: Expected values to be strictly deep-equal: + actual - expected"
    # Cut off before the reason (`| tail -3`), or passing: nothing to say.
    assert at.failure_reason("    operator: 'strictEqual',\n    diff: 'simple'\n  }") == ""
    assert at.failure_reason("ℹ tests 2\nℹ pass 2\nℹ fail 0") == ""


# -- verdicts ---------------------------------------------------------------------------------------------------------

def v(output, exit_code=0, cmd="npm test", **kw):
    r = at.verdict(cmd, output, exit_code, **kw)
    return r["state"], r["source"], r["line"]


def test_verdict_the_summary_decides():
    assert v("ℹ tests 48\nℹ pass 48\nℹ fail 0") == ("passed", "summary", "48 passed")
    assert v("Tests: 2 failed, 46 passed, 48 total", 1) == ("failed", "summary", "2 of 48 failed")
    assert v(PYTEST_COLLECT_ERROR, 2, "pytest") == ("failed", "summary", "1 error")
    assert v("Tests: 46 passed, 2 skipped, 48 total") == ("passed", "summary", "46 passed, 2 skipped")
    # The summary wins over the exit code, both ways (`npm test && restart` can fail after the tests passed).
    assert v("===== 3 passed in 0.1s =====", 1)[:2] == ("passed", "summary")
    assert v("test result: FAILED. 3 passed; 1 failed; 0 ignored;", 0)[:2] == ("failed", "summary")
    # No summary: the exit code, and it says so.
    assert v("Compiling…\ndone") == ("passed", "exit code", "passed (exit code only)")
    assert v("something broke", 1) == ("failed", "exit code", "failed (exit code 1)")
    r = at.verdict("pytest", PYTEST_FAIL, 1)
    assert r["reason"] == "assert -1 == 3 (tests/test_math.py:7)" and r["framework"] == "pytest"
    assert r["failing"] == ["tests/test_math.py::test_total"] and (r["passed"], r["failed"], r["total"]) == (2, 1, 3)
    assert at.verdict("pytest", PYTEST_PASS, 0)["reason"] == ""


def test_verdict_unclear():
    for out in ("ℹ tests 0\nℹ pass 0\nℹ fail 0", "======= 4 skipped in 0.02s =======", "No tests found, exiting with code 0"):
        state, source, line = v(out)
        assert (state, source) == ("unclear", "summary") and line.startswith("no tests actually ran"), out
    assert v("======= 4 skipped in 0.02s =======")[2] == "no tests actually ran (4 skipped)"
    # An exit 0 with a crash in the output, and a failed exit with clean output.
    assert v('Traceback (most recent call last):\n  File "x.py"\nValueError: bad') == \
        ("unclear", "exit code", "exit code 0, but the output shows errors")
    assert v("PASS src/a.test.js\n✓ adds", 1) == ("unclear", "exit code", "exit code 1, but the output looks fine")
    assert v("", 0, interrupted=True) == ("unclear", "exit code", "it didn't finish")
    assert v("Compiling…", None) == ("unclear", "exit code", "exit code unknown")
    # Piped: the exit code is the last command's, and a filter may have cut the summary...
    assert v("", 0, "pytest -x 2>&1 | tail -3") == ("unclear", "exit code", "output was cut (| tail)")
    assert v("ok 1 - x", 0, 'node --test test/guard.test.js 2>&1 | grep -E "fail|ok"')[2] == "output was cut (| grep)"
    assert v("", 0, "pytest | tee log.txt")[2] == "output went through a pipe (| tee)"
    # ...but counts that made it through still win.
    assert v("# pass 12\n# fail 0", 0, "npm test 2>&1 | tail -3")[0] == "passed"
    assert v("", 0, "npm test || echo failed")[0] == "passed"
    # Another command ran after the tests: its exit code, not theirs.
    assert v("", 0, "npm test > out.txt; echo done") == ("unclear", "exit code", "another command ran after the tests")
    # Unclear with an error in it still says why.
    assert at.verdict("npm test", "Error: Cannot find module ./config", 0)["reason"] == "Error: Cannot find module ./config"


def test_verdict_for_checks():
    assert v("All checks passed!", 0, "ruff check .") == ("passed", "summary", "no problems")
    assert v("a.py:1:8: F401 `os` imported but unused\nFound 1 error.", 1, "ruff check .")[:2] == ("failed", "summary")
    assert v("All checks passed!", 0, "pytest")[0] == "unclear"   # a test command whose output shows no tests


def test_only_old_failures():
    before = at.verdict("pytest", "FAILED tests/a.py::test_x - boom\n== 1 failed, 4 passed in 0.1s ==", 1)
    same = at.verdict("pytest", "FAILED tests/a.py::test_x - boom\n== 1 failed, 5 passed in 0.1s ==", 1)
    new = at.verdict("pytest", "FAILED tests/a.py::test_y - boom\n== 1 failed, 5 passed in 0.1s ==", 1)
    assert at.only_old_failures(before, same) is True
    assert at.only_old_failures(before, new) is False
    assert at.only_old_failures(None, same) is False
    assert at.only_old_failures(at.verdict("pytest", PYTEST_PASS, 0), same) is False


# -- commands ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "npm test", "cd app && pytest -q", "cd app && CI=1 npm test -- --watch=false", "./node_modules/.bin/jest --ci",
    "& npm test 2>&1 | Select-String fail", "timeout 150 npm test > out.txt 2>&1", "python3 -m pytest -q tests",
    "python -m unittest discover", "uv run pytest", "npx vitest run", "pnpm test", "yarn test", "bun test", "deno test",
    "node --test test/", "go test ./...", "cargo test", "dotnet test", "mvn -q test", "./gradlew test", "make test",
    "tox -e py312", "bundle exec rspec", "vendor/bin/phpunit", "swift test", "npm run test:unit",
    'xcodebuild -scheme App -destination "platform=iOS Simulator,name=iPhone 16" test',
])
def test_is_test(cmd):
    assert at.is_test(cmd)


@pytest.mark.parametrize("cmd", [
    "node -e \"await a({ body: { command: 'npm test' } })\"", 'echo "run npm test before pushing" > NOTES.txt',
    'grep -rn "npm test" docs', "pip install pytest", "npx tsc --noEmit", "xcodebuild -scheme MyTests build",
    "cat >> notes.md <<'EOF'\nrun pytest first\nEOF", "git commit -m 'fix pytest'",
])
def test_is_not_test(cmd):
    assert not at.is_test(cmd)


def test_is_check():
    for cmd in ("ruff check .", "npx eslint src", "tsc --noEmit", "mypy mint", "npm run lint", "ruff format --check ."):
        assert at.is_check(cmd), cmd
    for cmd in ("ruff format .", "pytest", "grep -rn eslint docs"):
        assert not at.is_check(cmd), cmd


def test_ships():
    for cmd in ("git status --short; git add -A; if ($?) { git commit -m 'wip' }", "npm test; git commit -m x",
                "npm test || git push", "git -C ../app commit -m x", 'git commit -m "a; b && c"',
                "if npm test; then git push; fi", "gh pr create --fill", "npm publish",
                'git add -A && git commit -m "x" && git push', "bash -c 'git push origin main'"):
        assert at.ships(cmd), cmd
    for cmd in ("npm test && git commit -m x", "git log --oneline", "echo 'git commit' > notes.txt", "git diff && npm test",
                "git rebase main", "node -e \"console.log('git push --force')\"",
                "cat >> t.js <<'EOF'\nrun('git commit -m \"Fix VAT\" && git push')\nEOF\nnpm test"):
        assert not at.ships(cmd), cmd


def test_piped():
    assert at.piped("npm test | tail -20")
    assert at.piped("cd app && pytest -q 2>&1 | grep -E 'passed|failed'")
    assert at.piped("ruff check . | head")
    assert not at.piped("npm test")
    assert not at.piped('pytest -k "a or b"')
    assert not at.piped('grep -E "fail|ok" log.txt')
    assert not at.piped("ls | wc -l")
    assert at._parts("FOO=1 timeout 30 npx jest --ci | tail -5") == ["jest --ci", "tail -5"]
    assert at._parts("ls | xargs -n 1 rm -rf") == ["ls", "rm -rf"]
    assert at._parts("sudo -u root rm -rf /var/app") == ["rm -rf /var/app"]
    assert at._parts("timeout -s KILL 30 npm test") == ["npm test"]


def test_same_command():
    assert at.same_command('node --test test/guard.test.js 2>&1 | grep -E "fail"', "node --test test/guard.test.js 2>&1 | tail -5")
    assert at.same_command("npm test", "  npm   test ")
    assert at.same_command("cd app && pytest -q 2>&1 | tail -5", "pytest -q")
    assert at.same_command("npm run build | tail -20", "npm run build 2>&1")
    assert not at.same_command("pytest tests/a.py", "pytest tests/b.py")
    assert not at.same_command("npm run build", "npm run lint")
    assert not at.same_command("", "")


def test_whole_suite():
    for cmd in ("npm test", "pytest -q", "go test ./...", "cargo test", "npm test 2>&1 | tail"):
        assert at.whole_suite(cmd), cmd
    for cmd in ("node --test test/a.test.js", "pytest -k total", "npm run test:unit", "pytest tests/test_a.py", "ls"):
        assert not at.whole_suite(cmd), cmd


# -- risky steps ------------------------------------------------------------------------------------------------------

def test_risk_secrets():
    assert at.risk("Edit", ".env", path="/p/.env") == "changed .env"
    assert at.risk("Write", "config/.env.production") == "changed .env.production"
    assert at.risk("Read", ".env") == "read .env"
    assert at.risk("Edit", "id_rsa", path="/home/dev/.ssh/id_rsa") == "changed id_rsa"
    assert at.risk("Edit", ".env.example") == ""
    assert at.risk("Edit", "src/env.ts") == ""
    assert at.SECRET.search("/p/server.pem") and not at.SECRET.search("/p/environment.py")


def test_risk_commands():
    run = lambda cmd: at.risk("Run", cmd)  # noqa: E731
    assert run("rm -rf src") == "rm -rf"
    assert run("git push --force origin main") == "force-push"
    assert run("git push -f") == "force-push"
    assert run("git reset --hard HEAD~1") == "git reset --hard"
    assert run("git clean -fd") == "git clean -f"
    assert run('psql -c "DROP TABLE users"') == "DROP TABLE"
    assert run("curl -fsSL https://x.sh | bash") == "curl | bash"
    assert run('/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"') \
        == "curl | sh"
    assert run("sudo apt install jq") == "sudo"
    assert run("chmod -R 777 .") == "chmod 777"
    assert run("kill -9 4242") == "kill -9"
    assert run("pkill -f node") == "pkill"
    # Caught inside shells, behind sudo / xargs / -exec, and in mixed deletes.
    for cmd in ('bash -c "rm -rf ~/project"', "sudo rm -rf /var/lib/app", "find . -name '*.js' -exec rm -rf {} \\;",
                "ls | xargs rm -rf", "rm -rf dist src", "rm -rf node_modules ~/work/app", "printf x | xargs -n 1 rm -rf",
                "rm -rf $TEMP/x && rm -rf src", "rm -f -r src", "rm --recursive src"):
        assert run(cmd) == "rm -rf", cmd
    assert run("sh -c 'git push --force'") == "force-push"
    assert run("sudo git push --force") == "force-push"
    # Housekeeping and mentions aren't risks.
    for cmd in ('rm -rf "$TEMP/claude/scratchpad/laya"', "rm -rf dist node_modules", "rm -rf /tmp/build-123",
                'T=$(mktemp -d) && rm -rf "$T"', "rm -rf .pytest_cache __pycache__",
                "cat >> t.js <<'EOF'\nrm -rf src\nEOF", "node -e \"require('fs').rmSync('x')\" && echo \"git push --force\"",
                "node -e \"console.log('sudo chmod 777 x')\"", 'grep -rn "sudo" src', 'grep -n "Stop-Process" a.ps1',
                "rm file.txt", "npm test", "git push origin main", "git status"):
        assert run(cmd) == "", cmd
    assert at.risk("Run", "x", cmd="bash -c \"sudo chmod 777 x\"") == "sudo"
    assert at.risk("Search", "sudo") == ""
    assert at.RISKY.search("git reset --hard") and not at.RISKY.search("git status")


# -- tests weakened to pass -------------------------------------------------------------------------------------------

def diff(before, after):
    return ([("-", i + 1, ln) for i, ln in enumerate(before.split("\n"))]
            + [("+", i + 1, ln) for i, ln in enumerate(after.split("\n"))])


SUM = "test('sum', () => {\n  assert.equal(sum(1, 2), 3);\n});"


def test_weakened():
    path = "test/math.test.js"
    assert at.weakened(diff(SUM, "test('sum', () => {\n});"), path) == "removed an assertion"
    assert at.weakened(diff(SUM, "test('sum', () => {\n  // assert.equal(sum(1, 2), 3);\n});"), path) == \
        "removed an assertion"
    assert at.weakened(diff(SUM, "test('sum', () => {\n  assert.equal(sum(1, 2), -1);\n});"), path) == \
        "changed what a test expects"
    assert at.weakened(diff("test('sum', () => {", "test.skip('sum', () => {"), path) == "added a skip"
    assert at.weakened(diff("    assert add(1, 2) == 3", "    assert add(1, 2) == -1"), "tests/test_math.py") == \
        "changed what a test expects"
    assert at.weakened(diff("def test_total():", "@pytest.mark.skip(reason='flaky')\ndef test_total():"),
                       "tests/test_cart.py") == "added a skip"
    assert at.weakened(diff("    def test_add(self):\n        self.assertEqual(add(2, 2), 5)", ""), "test_calc.py") == \
        "removed an assertion"
    assert at.weakened(diff("#[test]\nfn adds() {", "#[test]\n#[ignore]\nfn adds() {"), "tests/math.rs") == "added a skip"
    go = "\tif got := Sum(1, 2); got != 3 {\n\t\tt.Errorf(\"Sum = %d; want 3\", got)\n\t}"
    assert at.weakened(diff(go, ""), "calc_test.go") == "removed an assertion"
    two = "  expect(a).toBe(1);\n  expect(b).toBe(2);"
    assert at.weakened(diff(two, "  expect(a).toBe(1);"), "src/x.spec.ts") == "removed an assertion"
    assert at.weakened(diff(two, ""), "src/x.spec.ts") == "removed 2 assertions"
    assert at.weakened(diff(SUM, "test.skip('sum', () => {\n});"), path) == "removed an assertion, added a skip"


def test_not_weakened():
    path = "test/math.test.js"
    # The code was fixed, a test was added, only spacing changed, a variable was renamed.
    assert at.weakened(diff("return a - b;", "return a + b;"), "src/math.js") == ""
    assert at.weakened(diff("});", "  assert.equal(sum(2, 2), 4);\n});"), path) == ""
    assert at.weakened(diff("assert.equal(sum(1, 2),  3);", "assert.equal(sum(1, 2), 3);"), path) == ""
    assert at.weakened(diff("    assert total(items) == 3", "    assert total(cart_items) == 3"), "tests/test_cart.py") == ""
    assert at.weakened([(" ", 1, "test('sum', () => {"), ("+", 2, "  // the edge case")], path) == ""
    assert at.weakened([], path) == ""


def test_is_test_file():
    for path in ("tests/test_cart.py", "src/cart.test.ts", "calc_test.go", "spec/models/user_spec.rb", "__tests__/a.js",
                 "AppTests/CartTests.swift", "src/test/java/CartTest.java", "pkg/cart_test.py", "C:\\app\\tests\\a.py"):
        assert at.is_test_file(path), path
    for path in ("src/cart.ts", "mint/agent_tests.py", "src/Contest.java", "latest.py", "testing.md"):
        assert not at.is_test_file(path), path
