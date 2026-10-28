"""Physical authority and historical uncertainty feedback retain their ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadRunState
from bowhead_core.ordinary_lead_continuation import ordinary_failure_collection_enabled
from bowhead_router.invocation import physical_invocation_scope

pytestmark = pytest.mark.unit


def test_enclosing_physical_authority_cannot_be_bypassed_by_conversational_capture(monkeypatch):
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    ctx = SimpleNamespace(ordinary_lead_state=OrdinaryLeadRunState())
    assert ordinary_failure_collection_enabled(ctx)
    with physical_invocation_scope(SimpleNamespace(begin=AsyncMock())):
        assert not ordinary_failure_collection_enabled(ctx)


@pytest.mark.parametrize("outcome", ["unknown", "not_dispatched"])
def test_uncertain_feedback_publishes_explanation_without_retry_guidance(outcome):
    from bowhead_core.ordinary_lead_continuation import ordinary_execution_refusal
    from bowhead_core.subagent import _public_tool_result

    result = ordinary_execution_refusal()
    if outcome == "unknown":
        result = {"ok": False, "execution_outcome": "unknown", "directive": "Private model execution instructions"}
    public, admission = _public_tool_result("subagent_spawn", result)
    assert public["execution_outcome"] == outcome
    assert public["error"]
    if outcome == "not_dispatched":
        assert public["retryable"] is False
    assert "directive" not in public and admission is None
    assert "directive" in result
