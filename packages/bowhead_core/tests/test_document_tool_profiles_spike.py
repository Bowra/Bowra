"""The protocol comparison keeps runtime validation and question identities."""

import copy
import importlib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.unit


@pytest.fixture
def profiles(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    return importlib.import_module("spike_document_tool_profiles")


def valid_arguments(case):
    action = case["action"]
    if action == "list_questions":
        return {"action": action}
    result = {"action": action, "evidence": {"text": case["text"]}}
    if action == "answer":
        return {**result, "job_id": case["job_id"]}
    return {
        **result,
        "inputs": [
            {"artifact_id": file[0], "role": file[2], "current_message_position": index, "evidence": {"text": case["text"]}}
            for index, file in enumerate(case["files"], 1)
        ]
        + [{"artifact_id": file[0], "role": file[2], "evidence": {"text": case["text"]}} for file in case["pins"]],
        "requested_outputs": case["outputs"],
    }


def call(profiles, profile, arguments):
    name = profiles.ACTION_TOOLS[arguments["action"]][0] if profile == "split-actions" else "template_document_submit"
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


@pytest.mark.parametrize("profile", ["position-guidance", "refs-guidance", "split-actions"])
def test_all_profiles_advertise_the_complete_three_action_protocol(profiles, profile):
    tools = profiles.tools_for_profile(profile)[:-1]
    for tool in tools:
        Draft202012Validator.check_schema(tool["input_schema"])
    for case in profiles.CASES:
        arguments = valid_arguments(case)
        name = call(profiles, profile, arguments)["tool_calls"][0]["name"]
        tool = next(item for item in tools if item["name"] == name)
        assert Draft202012Validator(tool["input_schema"]).is_valid(arguments)
        assert profiles.score(case, call(profiles, profile, arguments), profile)["fixture_match"]
        assert not Draft202012Validator(tool["input_schema"]).is_valid({**arguments, "fabricated": True})


def test_split_actions_do_not_route_valid_arguments_to_the_wrong_handler(profiles):
    case = profiles.CASES[4]
    result = call(profiles, "split-actions", valid_arguments(case))
    result["tool_calls"][0]["name"] = "template_document_submit"
    assert profiles.score(case, result, "split-actions")["issue"] == "tool_action_mismatch"


def test_answer_scoring_rejects_obsolete_or_fabricated_job_identity(profiles):
    case = profiles.CASES[4]
    arguments = valid_arguments(case)
    for job_id in ("djob_synthetic_old", "djob_invented"):
        result = call(profiles, "split-actions", {**arguments, "job_id": job_id})
        assert profiles.score(case, result, "split-actions")["issue"] == "wrong_job_identity"


def test_ambiguous_followup_cannot_pass_by_answering_one_of_two_questions(profiles):
    case = profiles.CASES[5]
    result = call(profiles, "split-actions", {"action": "answer", "job_id": "djob_synthetic_red", "evidence": {"text": case["text"]}})
    assert profiles.score(case, result, "split-actions")["issue"] == "wrong_action"


def test_existing_pin_has_no_current_message_ordinal(profiles):
    case = profiles.CASES[1]
    arguments = valid_arguments(case)
    assert profiles.score(case, call(profiles, "split-actions", arguments), "split-actions")["fixture_match"]
    arguments["inputs"][-1]["current_message_position"] = 2
    assert not profiles.score(case, call(profiles, "split-actions", arguments), "split-actions")["fixture_match"]


def test_repeated_quotation_requires_an_exact_offset(profiles):
    case = {"action": "answer", "text": "Use Q2. Confirm: Use Q2.", "job_id": "djob_synthetic_active"}
    arguments = valid_arguments(case)
    arguments["evidence"] = {"text": "Use Q2."}
    assert profiles.score(case, call(profiles, "split-actions", arguments), "split-actions")["issue"] == "invalid_or_ambiguous_quotation"
    arguments["evidence"]["start"] = 0
    assert profiles.score(case, call(profiles, "split-actions", arguments), "split-actions")["fixture_match"]


def test_reachable_definitions_keep_nested_constraints_without_all_action_models(profiles):
    schema = profiles.tools_for_profile("split-actions")[0]["input_schema"]
    assert set(schema["$defs"]) == {"InputRoleProposal", "UserQuote"}
    assert schema["$defs"]["InputRoleProposal"]["additionalProperties"] is False
    assert schema["$defs"]["UserQuote"]["additionalProperties"] is False
    changed = copy.deepcopy(schema)
    arguments = valid_arguments(profiles.CASES[0])
    arguments["inputs"][0]["evidence"]["fabricated"] = True
    assert not Draft202012Validator(changed).is_valid(arguments)


def test_unknown_provider_outcome_has_no_failure_score_or_zero_usage(profiles):
    result = {"outcome_unknown": True, "error_type": "OpenAICompatError"}
    score = profiles.score(profiles.CASES[-1], result, "position-guidance")
    assert score["structural_valid"] is None and score["fixture_match"] is None
    assert score["issue"] == "provider_outcome_unknown"
    assert "usage" not in result
