"""Router retries inside a chat turn: one setting, two time budgets."""

import time

import pytest
from bowhead_core.agents._lead_context import reset_turn_deadline, set_turn_deadline
from bowhead_core.subagent import _TurnRetryObserver
from bowhead_core.team_llm_config import TeamLlmConfigError, _validate_retry, build_router_from_v2_config
from bowhead_router.errors import ProviderFailureKind
from bowhead_router.retry import DEFAULT_MAX_RETRIES, RetryNotice, configured_max_retries

pytestmark = pytest.mark.unit


class _Bus:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _notice(delay_s: float = 2.0, timeout_s: float | None = 60.0) -> RetryNotice:
    return RetryNotice(
        retry=1,
        max_retries=3,
        kind=ProviderFailureKind.TIMEOUT,
        delay_s=delay_s,
        mode="stream",
        provider="p",
        model="m",
        attempt_timeout_s=timeout_s,
    )


@pytest.mark.parametrize(
    ("turn_left", "call_left", "admitted"),
    [
        (120.0, 300.0, True),
        # Lead's wallclock is checked between steps: a retry may start shortly before it.
        (10.0, 300.0, True),
        (1.0, 300.0, False),
        # The hard per-call budget cancels an attempt as unknown: it must fit entirely.
        (120.0, 61.0, False),
    ],
)
async def test_retry_starts_before_the_turn_deadline_and_fits_the_call_budget(turn_left, call_left, admitted):
    bus = _Bus()
    observer = _TurnRetryObserver(bus=bus, turn_id="t_1", subagent_id="sub_1", profile="compute", call_deadline=time.monotonic() + call_left)
    token = set_turn_deadline(time.monotonic() + turn_left)
    try:
        assert await observer.admit_retry(_notice()) is admitted
        # The invoke fallback asks the same question with the last known attempt timeout.
        assert observer.attempt_fits(delay_s=2.0, attempt_timeout_s=None) is admitted
    finally:
        reset_turn_deadline(token)
    assert [event.event_type for event in bus.events] == (["llm.retry_scheduled"] if admitted else [])
    if admitted:
        event = bus.events[0]
        assert event.payload == {
            "subagent_id": "sub_1",
            "profile": "compute",
            "retry": 1,
            "max_retries": 3,
            "reason": "timeout",
            "delay_s": 2.0,
        }
        assert event.parent_agent_id == "sub_1"


def test_retry_setting_is_read_from_one_configuration_point() -> None:
    assert configured_max_retries({}) == DEFAULT_MAX_RETRIES == 3
    assert configured_max_retries({"retry": {"max_retries": 0}}) == 0
    assert configured_max_retries({"retry": {"max_retries": 9}}) == DEFAULT_MAX_RETRIES
    _validate_retry(None)
    _validate_retry({"max_retries": 5})
    for invalid in ({"max_retries": 6}, {"max_retries": True}, {"max_retries": "2"}, {"max_retries": 2, "extra": 1}, []):
        with pytest.raises(TeamLlmConfigError):
            _validate_retry(invalid)
    config = {
        "schema_version": 2,
        "providers": {"main": {"type": "openai_compatible", "base_url": "https://93.184.216.34/v1", "api_key": "k", "models": [{"id": "m"}]}},
        "tiers": {"primary": {"provider": "main", "model": "m"}},
        "retry": {"max_retries": 1},
    }
    assert build_router_from_v2_config(config).max_retries == 1
