"""Team agent run limits: validation, stored values, per-run time budget and the shared tool-call budget."""

import pytest

from bowhead_core import agent_run_limits as rl

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime_config(monkeypatch):
    rl.get_runtime_controls.cache_clear()
    yield
    rl.get_runtime_controls.cache_clear()


@pytest.mark.parametrize("system,team,expected", [(None, None, None), (1000, None, 1000), (None, 200, 200), (1000, 200, 200), (100, 200, 100)])
def test_effective_ceiling_preserves_the_saved_team_choice(system, team, expected):
    limits = rl.AgentRunLimits(step_limit=team)
    before = limits.to_json()
    controls = rl.AgentRuntimeControls(system_step_limit=system)
    assert controls.budget(limits).step_limit == expected
    assert controls.view(limits)["effective_step_limit"] == expected
    assert limits.to_json() == before


@pytest.mark.parametrize(
    "name,value",
    [
        ("BOWHEAD_AGENT_MAX_STEPS", "0"),
        ("BOWHEAD_AGENT_MAX_STEPS", "-1"),
        ("BOWHEAD_AGENT_MAX_STEPS", "true"),
        ("BOWHEAD_AGENT_MAX_STEPS", "1.5"),
        ("BOWHEAD_AGENT_MAX_STEPS", "10001"),
        ("BOWHEAD_AGENT_STALL_TIMEOUT_SEC", "29"),
        ("BOWHEAD_AGENT_STALL_TIMEOUT_SEC", "36001"),
        ("BOWHEAD_AGENT_STALL_TIMEOUT_SEC", ""),
    ],
)
def test_active_invalid_operator_values_fail_with_the_field_name(monkeypatch, runtime_config, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        rl.get_runtime_controls()


def test_runtime_policy_is_a_startup_snapshot(monkeypatch, runtime_config):
    controls = rl.get_runtime_controls()
    assert controls == rl.AgentRuntimeControls()
    monkeypatch.setenv("BOWHEAD_AGENT_MAX_STEPS", "40")
    assert rl.get_runtime_controls() is controls


def test_saved_runtime_budget_requires_a_version_and_valid_used_count():
    budget = rl.AgentRuntimeControls().budget(rl.AgentRunLimits(step_limit=3))
    raw = budget.model_dump()
    assert rl.AgentLoopBudget.model_validate({**raw, "model_steps_used": 3}).model_steps_used == 3
    for patch in ({"model_steps_used": 4}, {"model_steps_used": True}, {"contract": "unknown"}):
        with pytest.raises(ValueError):
            rl.AgentLoopBudget.model_validate({**raw, **patch})
    raw.pop("contract")
    with pytest.raises(ValueError):
        rl.AgentLoopBudget.model_validate(raw)


def test_defaults_are_thirty_minutes_unlimited_steps_and_300_tool_calls() -> None:
    assert rl.AgentRunLimits(turn_time_limit_s=1800, step_limit=None, tool_call_limit=300) == rl.DEFAULT_LIMITS
    assert rl.DEFAULT_LIMITS.wrap_up_reserve_s == 300


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"turn_time_limit_s": 599, "step_limit": None, "tool_call_limit": 300}, "turn_time_limit_s"),
        ({"turn_time_limit_s": 36_001, "step_limit": None, "tool_call_limit": 300}, "turn_time_limit_s"),
        ({"turn_time_limit_s": 1800, "step_limit": 0, "tool_call_limit": 300}, "step_limit"),
        ({"turn_time_limit_s": 1800, "step_limit": True, "tool_call_limit": 300}, "step_limit"),
        ({"turn_time_limit_s": 1800, "step_limit": None, "tool_call_limit": 10_001}, "tool_call_limit"),
        ({"turn_time_limit_s": "1800", "step_limit": None, "tool_call_limit": 300}, "turn_time_limit_s"),
    ],
)
def test_validation_names_the_field_and_range(payload: dict, field: str) -> None:
    with pytest.raises(rl.AgentRunLimitsInvalid) as exc:
        rl.validate(payload)
    assert exc.value.field == field


def test_stored_values_round_trip_and_bad_values_fall_back_to_defaults() -> None:
    limits = rl.AgentRunLimits(turn_time_limit_s=36_000, step_limit=None, tool_call_limit=900)
    assert rl.parse_stored(limits.to_json()) == limits
    assert '"step_limit":null' in limits.to_json()
    assert rl.parse_stored(None) == rl.DEFAULT_LIMITS
    assert rl.parse_stored("not json") == rl.DEFAULT_LIMITS
    assert rl.parse_stored('{"turn_time_limit_s": 5}') == rl.DEFAULT_LIMITS


@pytest.mark.parametrize("configured,remaining,expected", [(None, 1400, 1400), (900, 1400, 900), (900, 300, 300), (900, 0.2, 0.2), (None, -1, 0)])
def test_model_call_deadline_follows_remaining_time_or_explicit_limit(configured, remaining, expected) -> None:
    limits = rl.AgentRunLimits(model_call_time_limit_s=configured)
    assert rl.parse_stored(limits.to_json()).model_call_time_limit_s == configured
    assert rl.model_call_budget_s(limits, remaining_s=remaining) == expected


@pytest.mark.asyncio
async def test_store_errors_fall_back_to_defaults() -> None:
    class _Broken:
        async def get(self, team_id: str, key: str) -> str:
            raise RuntimeError("db locked")

    assert await rl.load_team_limits(_Broken(), "team_1") == rl.DEFAULT_LIMITS
    assert await rl.load_team_limits(None, "team_1") == rl.DEFAULT_LIMITS


def test_subagents_stop_five_minutes_before_the_turn_deadline_and_lead_uses_all_of_it() -> None:
    limits = rl.AgentRunLimits(turn_time_limit_s=3600)
    assert rl.run_time_budget_s(limits, is_lead=False, turn_deadline=1000.0 + 3600, now=1000.0) == 3300
    assert rl.run_time_budget_s(limits, is_lead=True, turn_deadline=1000.0 + 3600, now=1000.0) == 3600
    assert rl.run_time_budget_s(limits, is_lead=False, turn_deadline=1000.0 + 200, now=1000.0) == 0
    # Agent evaluation runs a subagent outside a Lead turn.
    assert rl.run_time_budget_s(limits, is_lead=False, turn_deadline=None, now=1000.0) == 3300


def test_unlimited_steps_are_recorded_as_none_not_as_the_loop_bound() -> None:
    assert rl.persisted_step_limit(rl.UNBOUNDED_STEPS) is None
    assert rl.persisted_step_limit(40) == 40


def test_one_tool_call_budget_counts_every_tool_but_never_blocks_returning() -> None:
    budget = rl.TurnToolCallBudget(limit=2)
    assert budget.admit("bi_query") and budget.admit("teammcp.crm_search")
    assert budget.admit("web_search") is False
    assert budget.admit("subagent_return") is True
    assert "2 tool calls" in budget.refusal()


def test_wrap_up_reply_splits_into_done_and_remaining() -> None:
    from bowhead_core.agent_run_wrap_up import parse_wrap_up

    assert parse_wrap_up("Done: 4 files read.\nRemaining: edit SKILL.md") == ("4 files read.", "edit SKILL.md")
    assert parse_wrap_up("**已完成**：读了 4 个文件\n**未完成**：修改 SKILL.md") == ("读了 4 个文件", "修改 SKILL.md")
    assert parse_wrap_up("Only a summary without headings") == ("Only a summary without headings", None)
    assert parse_wrap_up("") == (None, None)


@pytest.mark.asyncio
async def test_credit_gate_subtracts_this_turns_usage_and_rereads_balances_at_most_every_interval() -> None:
    reads = []
    now = [0.0]
    spent = [0]

    async def balances():
        reads.append(now[0])
        return 100, 1000

    gate = rl.TurnCreditGate(balances, lambda: spent[0], interval_s=30, clock=lambda: now[0])
    assert await gate.exhausted() is None
    spent[0] = 100
    assert await gate.exhausted() == "user_credit_exhausted"
    now[0] = 10
    assert await gate.exhausted() == "user_credit_exhausted"
    assert reads == [0.0]  # cached inside the interval
    now[0] = 31
    await gate.exhausted()
    assert reads == [0.0, 31]


@pytest.mark.asyncio
async def test_credit_gate_reports_the_team_pool_and_ignores_untracked_balances() -> None:
    async def team_only():
        return None, 50

    gate = rl.TurnCreditGate(team_only, lambda: 60)
    assert await gate.exhausted() == "team_pool_exhausted"

    async def failing():
        raise RuntimeError("db locked")

    assert await rl.TurnCreditGate(failing, lambda: 10**9).exhausted() is None
