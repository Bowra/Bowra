"""v2 local billing when a generation never recorded model usage.

A generation stopped before its first model call has zero tokens and no
``billing_usage_json``; it settles at zero credits. Recorded tokens without
classified usage remain malformed (fail closed).
"""

from __future__ import annotations

import json

import pytest
from bowhead_core.terminal_effect_contract import EFFECT_CONTRACT_VERSION
from bowhead_core.terminal_protocol import TerminalProtocolMalformedError, TerminalScope
from bowhead_core.terminal_protocol_store import _local_billing_fact_from_generation

pytestmark = pytest.mark.unit

_SCOPE = TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)


def _generation(*, input_tokens: int, output_tokens: int, billing_usage_json: str | None) -> dict[str, object]:
    return {
        "execution_intent_json": json.dumps({"effect_contract_version": EFFECT_CONTRACT_VERSION, "billing_rate_card_version": "v2"}),
        "session_user_id": "user_1",
        "session_workspace_id": "ws_1",
        "team_id": "team_1",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_amount": 0.0,
        "billing_usage_json": billing_usage_json,
    }


def test_zero_tokens_without_recorded_usage_settles_at_zero_credits() -> None:
    fact = _local_billing_fact_from_generation(_SCOPE, _generation(input_tokens=0, output_tokens=0, billing_usage_json=None))

    assert fact.rate_card_version == "v2"
    assert fact.model_usage == ()
    assert fact.cost_credits == 0


@pytest.mark.parametrize(("input_tokens", "output_tokens"), [(1, 0), (0, 1), (700, 300)])
def test_recorded_tokens_without_recorded_usage_fail_closed(input_tokens: int, output_tokens: int) -> None:
    with pytest.raises(TerminalProtocolMalformedError, match="v2 local billing model usage is unavailable"):
        _local_billing_fact_from_generation(
            _SCOPE,
            _generation(input_tokens=input_tokens, output_tokens=output_tokens, billing_usage_json=None),
        )
