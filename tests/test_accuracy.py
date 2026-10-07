"""Mint's coding-agent checks against real requests a person checked by hand (tests/accuracy/cases, from dotpals). A
claim that was right must stay right: no claim kind may have more wrong answers, or fewer right ones, than
tests/accuracy/baseline.json, and nothing wrong that isn't in it. After a real improvement,
`python tests/accuracy/score.py --save` records the new baseline."""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent / "accuracy"


def _score_module():
    spec = importlib.util.spec_from_file_location("mint_accuracy_score", HERE / "score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


score = _score_module()


def test_accuracy_holds_the_baseline():
    baseline = json.loads((HERE / "baseline.json").read_text(encoding="utf-8"))
    cases = score.load_cases()
    assert len(cases) >= 30, "the labeled cases are there"
    now = score.score_all(cases)
    fresh = [f"{score.key(w)}: said {w['said']!r}, really {w['truth']!r}" for w in now["wrong"]
             if score.key(w) not in set(baseline["wrong"])]
    assert fresh == []
    for kind, was in baseline["byKind"].items():
        k = now["byKind"][kind]
        assert k["wrong"] <= was["wrong"], f"{kind}: {k['wrong']} wrong, the baseline has {was['wrong']}"
        assert k["right"] >= was["right"], f"{kind}: {k['right']} right, the baseline has {was['right']}"


def test_risk_map_covers_every_dotpals_warning_in_the_cases():
    """Each warning a person labeled is one Mint's flags can be mapped to (or the risky score would skip it)."""
    labeled = {x for c in score.load_cases() for x in c["truth"].get("risky", [])}
    assert labeled and all(score._DOTPALS_WARNINGS.match(x) for x in labeled)


def test_why_match_is_fair():
    m = score._why_matches
    assert m("expected 3, got -1 (test/math.test.js:5)", "expected 3, got -1")
    assert m("AssertionError [ERR_ASSERTION]: Expected values to be strictly equal: 3 !== 2",
             "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:")
    assert m("SyntaxError: Unexpected token '.' (src/x.js:3)", "SyntaxError: Unexpected token '.'")
    assert not m("AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:",
                 "AssertionError [ERR_ASSERTION]: The input did not match the regular expression /x/")
    assert not m("expected 1, got 2", "expected 3, got -1")
