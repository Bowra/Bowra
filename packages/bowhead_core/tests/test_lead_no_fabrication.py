"""chat-v4 CHANGE-W4.4 — slot-optional rule in Lead prompt."""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt


@pytest.mark.unit
def test_empty_data_empty_payload() -> None:
    """Lead prompt must contain the slot-optional guidance (kpis/actions/insights
    may be empty when data unavailable — fabricate is BANNED).
    """
    prompt = build_lead_system_prompt(workspace_id="w_test", facts=[])
    assert "slot-optional" in prompt
    # Empty-array example must appear so LLM has concrete template.
    assert '"kpis": []' in prompt
    # "Never" fabricate to fill — anti-fabrication directive.
    assert "**Never** fabricate to fill" in prompt
