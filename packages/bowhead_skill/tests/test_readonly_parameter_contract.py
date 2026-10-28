"""Shared UI/server cases for the supported read_only_entry parameter subset."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_skill.readonly_entry import ReadOnlyEntryError, parse_read_only_entry

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3] / "contracts/skill-readonly-entry"
DECLARATION = json.loads((ROOT / "v1.json").read_text())
CASES = json.loads((ROOT / "parameter-validation-v1.json").read_text())


@pytest.mark.parametrize("case", CASES["valid_schemas"], ids=lambda case: case["name"])
def test_shared_valid_parameter_schemas(case):
    entry = parse_read_only_entry({**DECLARATION, "parameter_schema": case["schema"]})
    for values in case["valid_values"]:
        assert entry.validate_parameters(values) == values
    for values in case["invalid_values"]:
        with pytest.raises(ReadOnlyEntryError):
            entry.validate_parameters(values)


@pytest.mark.parametrize("case", CASES["invalid_schemas"], ids=lambda case: case["name"])
def test_shared_invalid_parameter_schemas(case):
    with pytest.raises(ReadOnlyEntryError):
        parse_read_only_entry({**DECLARATION, "parameter_schema": case["schema"]})
