"""Every tool declaration must be one the Live API accepts: one bad schema (an empty enum value) made the
Live session refuse to connect at all (30 Sep, error 1007 "enum[2]: cannot be empty")."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")


def _walk(schema, path, problems):
    if schema is None:
        return
    if schema.enum is not None:
        if not schema.enum:
            problems.append(f"{path}: empty enum list")
        problems += [f"{path}: empty enum value" for value in schema.enum if value == ""]
    for key, child in (schema.properties or {}).items():
        _walk(child, f"{path}.{key}", problems)
    if schema.items is not None:
        _walk(schema.items, f"{path}[]", problems)


def test_declarations_are_valid_for_live():
    from mint.tools import registry
    problems, names = [], []
    for tool in registry.tools():
        for decl in tool.function_declarations or []:
            names.append(decl.name)
            if not decl.description:
                problems.append(f"{decl.name}: no description")
            _walk(decl.parameters, decl.name, problems)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate tool names: {duplicates}"
    assert not problems, "; ".join(problems[:20])
