"""The proposal patch semantics shared with the frontend preview (`lib/agent-proposal-operations.ts`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, apply_operations

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/agent_change/apply_operations_v1.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_contract_is_versioned() -> None:
    assert _CASES["contract"] == "agent-change-apply-operations/v1"
    assert any("error" in case for case in _CASES["cases"])
    assert any("expected" in case for case in _CASES["cases"])


@pytest.mark.parametrize("case", _CASES["cases"], ids=[case["name"] for case in _CASES["cases"]])
def test_apply_operations_matches_shared_fixture(case: dict) -> None:
    document = case["document"]
    original = copy.deepcopy(document)
    if "error" in case:
        with pytest.raises(AgentChangeProposalError) as error:
            apply_operations(document, case["operations"])
        assert error.value.code == case["error"]
    else:
        assert apply_operations(document, case["operations"]) == case["expected"]
    # The input document is never changed, whether the patch applies or not.
    assert document == original
