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


def test_undo():
    """The undo journal: record, undo, redo, refusals, expiry (fake inverses, a temporary journal)."""
    run("undo")


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil and codesign are macOS tools")
def test_updater():
    """Updates: versions, release checks, and in-place swaps of a dummy app (signatures, refusals)."""
    run("updater")


def _wake_models() -> bool:
    """openWakeWord's feature models are downloaded by install.sh, not by pip."""
    spec = importlib.util.find_spec("openwakeword")
    if spec is None or not spec.origin:
        return False
    models = Path(spec.origin).parent / "resources" / "models"
    return (models / "melspectrogram.onnx").exists() and (models / "embedding_model.onnx").exists()


@pytest.mark.skipif(not _wake_models(), reason="openWakeWord feature models not downloaded (./install.sh fetches them)")
def test_wake_word_features():
    """The light-weight wake word features match openWakeWord exactly."""
    run("wake_features")


@pytest.mark.skipif(not os.environ.get("MINT_INTEGRATION"), reason="set MINT_INTEGRATION=1 (network, AppleScript)")
def test_harness_tools():
    """Files, guards, browser helpers, AppleScript and web tools, without the screen."""
    run("harness")
