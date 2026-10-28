"""Real declaration/schema/file checks; nothing here grants a runtime scope."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
import yaml
from bowhead_skill.loader import load_skill
from bowhead_skill.readonly_entry import (
    ReadOnlyEntry,
    ReadOnlyEntryError,
    parse_read_only_entry,
    validate_read_only_entry_files,
)

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).resolve().parents[3] / "contracts/skill-readonly-entry/v1.json"


@pytest.fixture
def declaration() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def bundle(root: Path, declaration: object) -> Path:
    root.mkdir(exist_ok=True)
    (root / "references").mkdir(exist_ok=True)
    (root / "references/method.md").write_text("# Profitability\nExplain margin and turnover from authorized data.\n")
    (root / "references/metrics.md").write_text("# Definitions\nROE = net margin × asset turnover × equity multiplier.\n")
    metadata = {"name": "readonly-example", "description": "Profitability method", "read_only_entry": declaration}
    (root / "SKILL.md").write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n# Package\n")
    return root


def test_loader_retains_immutable_declaration_and_existing_script_inventory(tmp_path, declaration):
    root = bundle(tmp_path, declaration)
    (root / "scripts").mkdir()
    (root / "scripts/legacy.py").write_text("raise RuntimeError('must not execute during loading')\n")
    spec = load_skill(root)
    assert spec.read_only_entry.to_dict() == declaration
    assert spec.frontmatter["read_only_entry"] == declaration
    assert spec.script_names == ["legacy.py"]
    assert spec.read_only_entry.required_files == ("references/metrics.md",)
    declaration["requires_script"] = True
    projected = spec.read_only_entry.to_dict()
    projected["parameter_schema"]["properties"]["limit"]["maximum"] = 999
    assert spec.read_only_entry.to_dict()["requires_script"] is False
    assert spec.read_only_entry.parameter_schema["properties"]["limit"]["maximum"] == 10
    with pytest.raises(FrozenInstanceError):
        spec.read_only_entry._canonical_json = "{}"


def test_validates_values_without_inserting_defaults_or_mutating_input(declaration):
    entry = parse_read_only_entry(declaration)
    values = {"period": "2026-09-13", "region": None, "adjustments": [{"label": "Revenue", "amount": 2.5}]}
    result = entry.validate_parameters(values)
    assert result == values
    assert "limit" not in result
    result["adjustments"][0]["amount"] = 99
    assert values["adjustments"][0]["amount"] == 2.5


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"period": "2026-02-30"},
        {"period": "2026-09-13", "unknown": "private-account"},
        {"period": "2026-09-13", "limit": True},
        {"period": "2026-09-13", "limit": 11},
        {"period": "2026-09-13", "region": "US"},
        {"period": "2026-09-13", "adjustments": [{"label": "x"}]},
        {"period": "2026-09-13", "adjustments": [{"label": "x", "amount": 1}] * 4},
        {"period": "2026-09-13", "limit": float("inf")},
        {"period": "2026-09-13", "adjustments": [{"label": "x", "amount": 2**53}]},
        {"period": "x" * (16 * 1024)},
    ],
)
def test_parameter_boundaries_are_finite_and_do_not_echo_values(declaration, values):
    with pytest.raises(ReadOnlyEntryError) as error:
        parse_read_only_entry(declaration).validate_parameters(values)
    assert str(error.value) in {"skill_readonly_invalid_parameters", "skill_readonly_invalid_json", "skill_readonly_json_limit"}
    assert "private-account" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("requires_script", True),
        ("requires_script", 0),
        ("requires_new_tools", "false"),
        ("requires_new_connections", None),
        ("extra", "ignored?"),
    ],
)
def test_explicit_closed_declaration_is_required(declaration, field, value):
    declaration[field] = value
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_declaration"):
        parse_read_only_entry(declaration)


def test_missing_declaration_is_legacy_but_explicit_null_is_invalid(tmp_path, declaration):
    root = bundle(tmp_path, declaration)
    (root / "SKILL.md").write_text("---\nname: legacy\ndescription: Method\nkind: knowledge\n---\n# Method\n")
    assert load_skill(root).read_only_entry is None
    bundle(root, None)
    with pytest.raises(ReadOnlyEntryError):
        load_skill(root)
    with pytest.raises(ReadOnlyEntryError):
        ReadOnlyEntry('{"requires_script":false}')


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/outside.md",
        "references/../outside.md",
        "references//method.md",
        "./references/method.md",
        "references\\method.md",
        "scripts/reference.md",
        "Scripts/reference.md",
        "references/run.py",
        "references/index.html",
        "references/\x00method.md",
        "C:/method.md",
    ],
)
def test_declared_files_have_portable_text_only_paths(declaration, path):
    declaration["entry_file"] = path
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        parse_read_only_entry(declaration)


def test_all_declaration_fields_and_file_identity_are_required(declaration):
    for field in declaration:
        value = dict(declaration)
        del value[field]
        with pytest.raises(ReadOnlyEntryError):
            parse_read_only_entry(value)
    declaration["required_files"] += ["REFERENCES/METHOD.md"]
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        parse_read_only_entry(declaration)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}, "additionalProperties": True},
        {"type": "object", "properties": {}, "additionalProperties": False, "required": ["missing"]},
        {"type": "string"},
        {"type": "object", "properties": {"x": {"type": "string", "format": "password"}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": "integer", "default": "1"}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": "string", "enum": []}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": "string", "enum": ["ok", 1]}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": ["string", "number"]}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": "array"}}, "additionalProperties": False},
        {"type": "object", "properties": {"x": {"type": "string", "writeOnly": True}}, "additionalProperties": False},
    ],
)
def test_unsupported_and_invalid_parameter_schema_is_not_silently_accepted(declaration, schema):
    declaration["parameter_schema"] = schema
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_schema"):
        parse_read_only_entry(declaration)


def test_remote_refs_under_defaults_are_rejected_before_any_resolver_call(declaration, monkeypatch):
    calls = []

    def forbid(*args, **kwargs):
        calls.append(args)
        raise AssertionError("schema validation attempted network")

    monkeypatch.setattr("urllib.request.urlopen", forbid)
    declaration["parameter_schema"] = {
        "type": "object",
        "properties": {"x": {"type": "string", "$ref": "https://invalid.example/private-schema"}},
        "additionalProperties": False,
        "default": {"x": "private-value"},
    }
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_schema"):
        parse_read_only_entry(declaration)
    assert calls == []


@pytest.mark.parametrize("change", ["missing", "directory", "file-link", "directory-link", "binary", "size", "encoding"])
def test_admission_checks_the_current_declared_files(tmp_path, declaration, change):
    root = bundle(tmp_path / "bundle", declaration)
    target = root / "references/metrics.md"
    target.unlink()
    if change == "directory":
        target.mkdir()
    elif change in {"file-link", "directory-link"}:
        outside = tmp_path / "private.md"
        outside.write_text("private file")
        if change == "file-link":
            target.symlink_to(outside)
        else:
            (root / "references/method.md").unlink()
            (root / "references").rmdir()
            (tmp_path / "method.md").write_text("outside method")
            (tmp_path / "metrics.md").write_text("outside metrics")
            (root / "references").symlink_to(tmp_path, target_is_directory=True)
    elif change == "binary":
        target.write_bytes(b"plain\x00data")
    elif change == "size":
        target.write_bytes(b"a" * (256 * 1024 + 1))
    elif change == "encoding":
        target.write_bytes(b"\xff")
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        validate_read_only_entry_files(load_skill(root).read_only_entry, root)


def test_file_count_total_and_json_budgets(tmp_path, declaration):
    root = bundle(tmp_path, declaration)
    declaration["required_files"] = [f"references/{i}.txt" for i in range(5)]
    for path in declaration["required_files"]:
        (root / path).write_bytes(b"a" * (256 * 1024))
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        validate_read_only_entry_files(parse_read_only_entry(declaration), root)
    declaration["required_files"] = [f"references/{i}.txt" for i in range(33)]
    with pytest.raises(ReadOnlyEntryError):
        parse_read_only_entry(declaration)
    cyc = {}
    cyc["self"] = cyc
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_json_limit"):
        parse_read_only_entry(cyc)


@pytest.mark.parametrize("field,value", [("kind", "agent"), ("source_kind", "global"), ("id", "bad/id"), ("version", "latest"), ("scope", "all")])
def test_dependencies_are_exact_skill_refs(declaration, field, value):
    declaration["required_skill_refs"][0][field] = value
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_dependency"):
        parse_read_only_entry(declaration)


def test_dependency_identity_cannot_name_two_versions(declaration):
    other = dict(declaration["required_skill_refs"][0], version="sha256:" + "b" * 64)
    declaration["required_skill_refs"].append(other)
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_dependency"):
        parse_read_only_entry(declaration)


def test_empty_entry_is_not_a_method(tmp_path: Path, declaration: dict[str, Any]) -> None:
    root = bundle(tmp_path, declaration)
    (root / "references/method.md").write_text(" \n\t")
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        validate_read_only_entry_files(load_skill(root).read_only_entry, root)


def test_parameter_failure_traceback_does_not_expose_values(declaration: dict[str, Any]) -> None:
    import traceback

    entry = parse_read_only_entry(declaration)
    marker = "private" + "-customer-parameter-123"
    values = {"period": "2026-09-13", "region": marker}
    with pytest.raises(ReadOnlyEntryError) as captured:
        entry.validate_parameters(values)
    assert marker not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize("limit", ["dependencies", "schema-nodes", "file-count"])
def test_structural_limits_use_valid_unique_fixtures(declaration: dict[str, Any], limit: str) -> None:
    if limit == "dependencies":
        template = declaration["required_skill_refs"][0]
        declaration["required_skill_refs"] = [dict(template, id=f"method-{index}") for index in range(17)]
    elif limit == "schema-nodes":
        child = {"type": "object", "properties": {f"f{index}": {"type": "boolean"} for index in range(64)}, "additionalProperties": False}
        declaration["parameter_schema"] = {"type": "object", "properties": {"one": child, "two": child}, "additionalProperties": False}
    else:
        declaration["required_files"] = [f"references/{index}.txt" for index in range(33)]
    with pytest.raises(ReadOnlyEntryError):
        parse_read_only_entry(declaration)


def test_pending_metadata_is_loadable_but_admission_requires_all_files(tmp_path, declaration):
    root = bundle(tmp_path, declaration)
    (root / "references/method.md").unlink()
    entry = load_skill(root).read_only_entry
    assert entry.to_dict() == declaration
    validate_read_only_entry_files(entry, root, allow_missing=True)
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        validate_read_only_entry_files(entry, root)
    # Authoring's missing-file allowance does not waive present-file validation.
    (root / "references/metrics.md").write_bytes(b"\x00")
    with pytest.raises(ReadOnlyEntryError, match="skill_readonly_invalid_file"):
        validate_read_only_entry_files(entry, root, allow_missing=True)
