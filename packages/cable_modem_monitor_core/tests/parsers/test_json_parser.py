"""Tests for JSONParser.

Fixture-driven tests with synthesized JSON data. Each fixture contains
a JSON response, a JSONSection config, and expected channel output.
No modem-specific references.

Adding a test case = drop a JSON file in fixtures/json_parser/valid/.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from solentlabs.cable_modem_monitor_core.models.parser_config.json_format import (
    JSONSection,
)
from solentlabs.cable_modem_monitor_core.parsers.formats.json_parser import (
    JSONParser,
    _navigate_path,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "json_parser"
VALID_FIXTURES = sorted((FIXTURES_DIR / "valid").glob("*.json"))


def _load_fixture(path: Path) -> dict[str, Any]:
    """Load a JSON fixture file."""
    return dict(json.loads(path.read_text()))


def _build_resources(
    json_data: Any,
    resource_key: str,
) -> dict[str, Any]:
    """Build a resource dict. Returns empty dict if json_data is None."""
    if json_data is None:
        return {}
    return {resource_key: json_data}


@pytest.mark.parametrize(
    "fixture_path",
    VALID_FIXTURES,
    ids=[f.stem for f in VALID_FIXTURES],
)
def test_extraction(fixture_path: Path) -> None:
    """Parse JSON data and verify extracted channels match expected."""
    data = _load_fixture(fixture_path)

    # Multi-resource fixtures use _resources dict; single-resource use _resource + _json
    if "_resources" in data:
        resources: dict[str, Any] = data["_resources"]
    else:
        resource_key = data["_resource"]
        resources = _build_resources(data.get("_json"), resource_key)

    section_config = JSONSection.model_validate(data["_config"])
    parser = JSONParser(section_config)

    result = parser.parse(resources)
    expected = data["_expected"]

    assert result == expected, (
        f"Mismatch for {fixture_path.stem}:\n" f"  actual:   {result}\n" f"  expected: {expected}"
    )


NAVIGATE_PATH_CASES = [
    ("dict key", {"a": {"b": 1}}, "a.b", 1),
    ("nested dict", {"a": {"b": {"c": 2}}}, "a.b.c", 2),
    ("single key", {"x": 42}, "x", 42),
    ("missing key", {"a": 1}, "b", None),
    ("missing nested key", {"a": {"b": 1}}, "a.c", None),
    ("list index", {"_raw": [{"id": 1}, {"id": 2}]}, "_raw.0", {"id": 1}),
    ("list second element", {"_raw": [{"id": 1}, {"id": 2}]}, "_raw.1", {"id": 2}),
    ("list then key", {"_raw": [{"id": 1}]}, "_raw.0.id", 1),
    ("index out of bounds", {"_raw": [{"id": 1}]}, "_raw.5", None),
    ("index on dict", {"a": {"b": 1}}, "a.0", None),
    ("key on list", {"a": [1, 2]}, "a.x", None),
]


@pytest.mark.parametrize(
    ("label", "data", "path", "expected"),
    NAVIGATE_PATH_CASES,
    ids=[c[0] for c in NAVIGATE_PATH_CASES],
)
def test_navigate_path(label: str, data: Any, path: str, expected: Any) -> None:
    """_navigate_path handles dict keys and list indices."""
    assert _navigate_path(data, path) == expected
