"""`mint --doctor`: one line per check, the key shown only as ••••last4, a ✗ makes the exit code non-zero, and one
check that breaks never stops the rest. No network and no real key: the Google check is replaced."""
import time

import pytest

try:
    from mint.app import doctor
except ImportError:
    from mint import doctor

FAKE = "AIza" + "TEST" * 7 + "1234"   # (split, so the export's key scan doesn't flag it)
MARKS = (doctor.OK, doctor.WARN, doctor.BAD)


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    """A made-up key, Google never asked, and no real support folders to measure."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.setattr(doctor, "_verify", lambda key, timeout=doctor.NET_TIMEOUT: ("ok", ""))
    monkeypatch.setattr(doctor, "RUNTIMES", (("Mint", tmp_path / "Mint"), ("Hey Mint", tmp_path / "Hey Mint")))
    return monkeypatch


def _lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line[:1] in MARKS]


def test_one_line_per_check_and_an_int(quiet, capsys):
    code = doctor.run()
    out = capsys.readouterr().out
    assert isinstance(code, int)
    lines = _lines(out)
    assert len(lines) == len(doctor.CHECKS)
    for (title, _check), line in zip(doctor.CHECKS, lines, strict=True):
        assert line[2:].startswith(title)
    assert FAKE not in out
    key_line = next(line for line in lines if line[2:].startswith("Gemini key"))
    assert key_line.startswith(doctor.OK) and "••••1234" in key_line


def test_a_bad_key_is_a_cross_and_a_non_zero_exit(quiet, capsys):
    quiet.setattr(doctor, "_verify", lambda key, timeout=0: ("bad", "Google says this key isn't valid."))
    assert doctor.run() != 0
    out = capsys.readouterr().out
    line = next(line for line in _lines(out) if line[2:].startswith("Gemini key"))
    assert line.startswith(doctor.BAD) and "→" in line          # what to do about it
    assert FAKE not in out


def test_no_key_is_a_cross(quiet, capsys):
    quiet.delenv("GEMINI_API_KEY", raising=False)
    assert doctor.run() == 1
    assert any(line.startswith(doctor.BAD + " Gemini key") for line in _lines(capsys.readouterr().out))


def test_offline_is_only_a_warning(quiet):
    quiet.setattr(doctor, "_verify", lambda key, timeout=0: ("offline", "no answer"))
    mark, words, hint = doctor.check_key()
    assert mark == doctor.WARN and hint and FAKE not in words


def test_the_google_check_gives_up_quickly(monkeypatch):
    try:
        from mint.ui import onboarding
    except ImportError:
        from mint import onboarding
    monkeypatch.setattr(onboarding, "_verify_key", lambda key: time.sleep(3) or ("ok", ""))
    started = time.monotonic()
    assert doctor._verify(FAKE, timeout=0.2)[0] == "offline"
    assert time.monotonic() - started < 2


def test_a_broken_check_never_stops_the_rest(quiet, capsys):
    def boom():
        raise RuntimeError("no such thing")
    quiet.setattr(doctor, "CHECKS", (("First", boom), ("Second", lambda: (doctor.BAD, "broken", "fix it")),
                                      ("Third", lambda: (doctor.OK, "fine", ""))))
    assert doctor.run() == 1
    lines = _lines(capsys.readouterr().out)
    assert [line[:1] for line in lines] == [doctor.WARN, doctor.BAD, doctor.OK]
    assert "no such thing" in lines[0] and "fix it" in lines[1]


def test_telegram_on_without_a_token_is_a_cross(monkeypatch):
    try:
        from mint.core import prefs
    except ImportError:
        from mint import prefs
    monkeypatch.setattr(prefs, "get", lambda key: key == "telegram_enabled")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert doctor.check_telegram()[0] == doctor.BAD


def test_the_key_is_masked():
    assert doctor._mask(FAKE) == "••••1234"
    assert doctor._mask("short") == "••••"
