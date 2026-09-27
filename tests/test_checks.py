"""Runs the check scripts in tests/checks. Each prints PASS/FAIL lines and exits non-zero on failure."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

CHECKS = Path(__file__).parent / "checks"


def run(name: str) -> None:
    result = subprocess.run([sys.executable, str(CHECKS / f"{name}.py")], capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


def test_autopilot():
    """When Mint keeps going on a multi-part request, and when it must stop."""
    run("autopilot")


@pytest.mark.skipif(importlib.util.find_spec("openwakeword") is None, reason="openwakeword not installed")
def test_wake_word_features():
    """The light-weight wake word features match openWakeWord exactly."""
    run("wake_features")


@pytest.mark.skipif(not os.environ.get("MINT_INTEGRATION"), reason="set MINT_INTEGRATION=1 (network, AppleScript)")
def test_harness_tools():
    """Files, guards, browser helpers, AppleScript and web tools, without the screen."""
    run("harness")
