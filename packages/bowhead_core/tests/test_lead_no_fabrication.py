"""chat-v4 CHANGE-W4.4 — slot-optional rule in Lead prompt.

f36a94720 rewrote the rule as "Optional report content": KPI cards have no required count and are
included only when supported, so an empty or omitted `kpis` stays valid instead of being filled.
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt


@pytest.mark.unit
def test_empty_data_empty_payload() -> None:
    """Lead prompt must contain the slot-optional guidance (report cards may be empty
    when data is unavailable — never filled with unsupported content).
    """
    prompt = build_lead_system_prompt(workspace_id="w_test", facts=[])
    assert "<slot_optional_rule>" in prompt
    assert "KPI cards have no required count" in prompt
    # Cards only when supported by data: the anti-fabrication directive.
    assert "Include them only when useful and supported" in prompt
    # An empty or omitted kpis is an accepted shape.
    assert "omit `kpis` or use `[]`" in prompt
