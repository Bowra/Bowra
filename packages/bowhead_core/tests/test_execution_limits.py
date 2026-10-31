"""Team limits remain isolated and cannot exceed deployment resource boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.execution_limits import (
    ExecutionLimits,
    ExecutionLimitsInvalid,
    defaults,
    execution_limits_scope,
    load_team_limits,
    parse_stored,
    validate,
)
from bowhead_core.html_preview_policy import html_preview_limit_bytes
from bowhead_skill.execution_policy import sandbox_default_timeout_s, sandbox_max_timeout_s


@pytest.fixture(autouse=True)
def policy_environment(monkeypatch):
    for name in ("BOWHEAD_SANDBOX_MAX_TIMEOUT", "BOWHEAD_SANDBOX_DEFAULT_TIMEOUT_S", "BOWHEAD_SKILL_WALL_TIMEOUT_S", "BOWHEAD_CANVAS_HTML_MAX_BYTES"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_and_lowered_deployment_ceiling(monkeypatch):
    assert defaults() == ExecutionLimits(120, 600, 25 * 1024 * 1024)
    saved = ExecutionLimits(400, 500, 8 * 1024 * 1024).to_json()
    monkeypatch.setenv("BOWHEAD_SANDBOX_MAX_TIMEOUT", "300")
    assert parse_stored(saved) == ExecutionLimits(300, 300, 8 * 1024 * 1024)
    monkeypatch.setenv("BOWHEAD_SKILL_WALL_TIMEOUT_S", "180")
    assert parse_stored(saved).sandbox_max_timeout_s == 120
    assert parse_stored("null") == defaults()


def test_saved_html_override_survives_default_increase():
    saved = ExecutionLimits(120, 600, 5 * 1024 * 1024).to_json()
    assert parse_stored(saved).html_max_bytes == 5 * 1024 * 1024
    assert parse_stored(None).html_max_bytes == 25 * 1024 * 1024


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"sandbox_default_timeout_s": True}, "sandbox_default_timeout_s"),
        ({"sandbox_default_timeout_s": 301}, "sandbox_default_timeout_s"),
        ({"sandbox_max_timeout_s": 601}, "sandbox_max_timeout_s"),
        ({"html_max_bytes": 50 * 1024 * 1024 + 1}, "html_max_bytes"),
        ({"html_max_bytes": 0}, "html_max_bytes"),
    ],
)
def test_invalid_values_name_the_field(patch, field):
    with pytest.raises(ExecutionLimitsInvalid) as failure:
        validate({"sandbox_default_timeout_s": 120, "sandbox_max_timeout_s": 300, "html_max_bytes": 5242880, **patch})
    assert failure.value.field == field


@pytest.mark.asyncio
async def test_sibling_tasks_and_nested_scopes_do_not_leak_policy():
    async def read(policy):
        with execution_limits_scope(policy):
            await asyncio.sleep(0)
            return sandbox_default_timeout_s(), sandbox_max_timeout_s(), html_preview_limit_bytes()

    a, b = await asyncio.gather(read(ExecutionLimits(30, 60, 1024)), read(ExecutionLimits(240, 500, 8388608)))
    assert a == (30, 60, 1024)
    assert b == (240, 500, 8388608)
    assert (sandbox_default_timeout_s(), sandbox_max_timeout_s(), html_preview_limit_bytes()) == (120, 600, 25 * 1024 * 1024)


@pytest.mark.asyncio
async def test_team_identity_and_policy_read_failure():
    store = SimpleNamespace(get=AsyncMock(return_value=ExecutionLimits(30, 60, 1024).to_json()))
    assert (await load_team_limits(store, "team_a")).sandbox_max_timeout_s == 60
    store.get.assert_awaited_once_with("team_a", "execution_limits")
    store.get.side_effect = RuntimeError("Policy store unavailable")
    with pytest.raises(RuntimeError, match="Policy store unavailable"):
        await load_team_limits(store, "team_a")
