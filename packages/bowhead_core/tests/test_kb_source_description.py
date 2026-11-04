"""Optional description generation respects source, model and physical-call bounds."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bowhead_core.kb.source_description import (
    DescriptionAssignment,
    description_assignment,
    description_request,
    generate_description,
    validate_description,
)
from bowhead_router.token_estimator import estimate_tokens

pytestmark = pytest.mark.unit


def config():
    return {
        "providers": {
            "team": {"type": "openai_compatible", "base_url": "https://models.invalid", "models": [{"id": "text-a", "capabilities": ["chat"]}]}
        },
        "kb": {
            "models": {"source": {"capability": "source_description", "provider": "team", "real_id": "text-a"}},
            "active": {"source_description": "source"},
        },
    }


def test_descriptions_are_default_disabled_and_ignore_environment_assignment(monkeypatch):
    monkeypatch.setenv("BOWHEAD_KB_SOURCE_DESCRIPTION_MODEL", "source")
    assert description_assignment({}) is None
    assert description_assignment(None) is None
    value = config()
    assigned = description_assignment(value)
    value["providers"]["team"]["models"].append({"id": "unrelated", "capabilities": ["chat"]})
    assert description_assignment(value) == assigned
    value["providers"]["team"]["models"][0]["capabilities"] = ["embed"]
    with pytest.raises(ValueError, match="assignment_invalid"):
        description_assignment(value)


def test_source_request_bounds_and_output_support():
    assignment = DescriptionAssignment("team", "text-a", "fingerprint")
    file = SimpleNamespace(active_generation_id="generation-a", filename="Travel policy", chunks_count=100)
    chunks = [(f"chunk-{index}", "Travel reimbursement limits and approval. " * 1000, {"generation_id": "generation-a"}) for index in range(30)]
    request, references, source = description_request(file, chunks, assignment)
    assert len(references) == 12
    assert request.max_output_tokens == 2048
    assert estimate_tokens(request.system) + estimate_tokens(request.messages[0].content) + 128 <= 6000
    assert not request.tools
    value = {"description": "Travel reimbursement and approvals.", "topics": ["Travel"], "supporting_chunks": ["chunk-0"]}
    assert validate_description(json.dumps(value), references, source).description == value["description"]
    value["topics"] = ["Policy ABC-999"]
    with pytest.raises(ValueError, match="identifier_unsupported"):
        validate_description(json.dumps(value), references, source)
    value["topics"] = []
    value["supporting_chunks"] = ["hidden-chunk"]
    with pytest.raises(ValueError, match="output_invalid"):
        validate_description(json.dumps(value), references, source)


@pytest.mark.parametrize(("retries", "attempts"), [(0, 1), (1, 2), (5, 2)])
async def test_exact_target_has_no_fallback_and_at_most_two_physical_attempts(retries, attempts):
    router = SimpleNamespace(bind=Mock(), invoke=AsyncMock(return_value="result"), max_retries=retries)
    assignment = DescriptionAssignment("team", "text-a", "fingerprint")
    assert await generate_description(router, assignment, SimpleNamespace()) == "result"
    assert router.bind.call_args.args[0].fallback == []
    assert router.invoke.call_args.kwargs["target"] == ("team", "text-a")
    assert router.invoke.call_args.kwargs["retry_policy"].max_attempts_per_target == attempts
    router.invoke.assert_awaited_once()
