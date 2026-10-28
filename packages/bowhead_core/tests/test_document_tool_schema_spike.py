"""Schema experiments preserve admission boundaries and score failures honestly."""

import copy
import importlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.unit


@pytest.fixture
def spike(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    return importlib.import_module("spike_document_tool_schema")


def arguments(case):
    return {
        "action": "generate",
        "evidence": {"text": case["text"]},
        "inputs": [
            {"artifact_id": file[0], "role": file[2], "current_message_position": index, "evidence": {"text": case["text"]}}
            for index, file in enumerate(case["files"], 1)
        ],
        "requested_outputs": case["outputs"],
    }


@pytest.mark.parametrize("variant", ["current", "inline-union", "explicit-properties", "generation-object", "inline-properties", "position-guidance"])
def test_variants_accept_generation_but_reject_nested_strings(spike, variant):
    schema = spike.advertisement(variant)["input_schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    valid = arguments(spike.CASES[0])
    assert validator.is_valid(valid)
    for field in ("evidence", "inputs", "requested_outputs"):
        malformed = {**valid, field: json.dumps(valid[field])}
        assert not validator.is_valid(malformed), f"Stringified {field} must remain invalid"


@pytest.mark.parametrize("variant", ["current", "inline-union", "explicit-properties", "inline-properties", "position-guidance"])
def test_union_variants_keep_question_action_boundaries(spike, variant):
    validator = Draft202012Validator(spike.advertisement(variant)["input_schema"])
    assert validator.is_valid({"action": "list_questions"})
    assert not validator.is_valid({"action": "list_questions", "inputs": []})
    answer = {"action": "answer", "job_id": "djob_synthetic", "evidence": {"text": "Use last quarter."}}
    assert validator.is_valid(answer)
    assert not validator.is_valid({**answer, "requested_outputs": ["source"]})
    assert not validator.is_valid({"action": "answer", "job_id": "djob_synthetic"})


def test_generation_object_is_not_a_complete_action_union_replacement(spike):
    schema = spike.advertisement("generation-object")["input_schema"]
    assert "oneOf" not in schema and "$defs" not in schema
    assert schema["properties"]["inputs"]["type"] == "array"
    assert not Draft202012Validator(schema).is_valid({"action": "list_questions"})


@pytest.mark.parametrize("case_index", [0, 1])
def test_scoring_separates_valid_types_from_wrong_attachment_positions(spike, case_index):
    case = spike.CASES[case_index]
    valid = arguments(case)
    response = {"tool_calls": [{"name": "template_document_submit", "arguments": valid}]}
    assert spike.score(case, response)["semantic_valid"]
    malformed = copy.deepcopy(valid)
    malformed["inputs"][0]["current_message_position"] = 2
    score = spike.score(case, {"tool_calls": [{"name": "template_document_submit", "arguments": malformed}]})
    assert score["structural_valid"] and not score["semantic_valid"]


def test_scoring_does_not_repair_nested_strings_or_count_other_tools(spike):
    case = spike.CASES[0]
    malformed = arguments(case)
    malformed["inputs"] = json.dumps(malformed["inputs"])
    score = spike.score(case, {"tool_calls": [{"name": "template_document_submit", "arguments": malformed}]})
    assert not score["structural_valid"] and score["nested_types"]["inputs"] == "str"
    assert not spike.score(case, {"tool_calls": [{"name": "read_file", "arguments": {}}]})["semantic_valid"]


def test_unknown_references_do_not_become_silent_schema_relaxation(spike):
    with pytest.raises(ValueError, match="only local document definitions"):
        spike.inline_local_refs({"$ref": "https://example.invalid/schema"}, {})


def test_remote_diagnostic_has_valid_syntax(spike):
    compile(spike.REMOTE_PROBE, "document-schema-diagnostic", "exec")
