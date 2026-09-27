"""Every module in the package imports: catches a missing file or package in a checkout."""
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULES = sorted(".".join(p.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
                 for p in (ROOT / "mint").rglob("*.py") if p.name != "__main__.py")


def test_packages_present():
    for package in ("app", "core", "voice", "knowledge", "tools", "screen", "ui", "agents"):
        assert (ROOT / "mint" / package / "__init__.py").exists(), f"mint/{package} is missing"
    assert list((ROOT / "mint" / "resources" / "skills").rglob("*.md")), "example skills are missing"


@pytest.mark.parametrize("name", MODULES)
def test_import(name):
    importlib.import_module(name)
