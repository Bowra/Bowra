"""chat-v4 CTO P0-1 fix UT — web_search.allowed_roles includes 'lead'.

CTO finding on commit f916d30: `tool_aliases.yaml` had `web_search.allowed_roles=[analyst]`
so Lead (the only orchestrator that should fetch external context to compose a
turn-plan) was rejected with ToolPermissionError at runtime. yaml fix +
codegen alone is half the fix; this test invokes `ToolRegistry.execute` with
`ctx.role='lead'` and asserts the call does NOT short-circuit with
`ToolPermissionError`. We mock the dashscope HTTP call so the test is hermetic.

Companion: `test_web_search.py` covers handler unit behavior; this file covers
the role-gate at registry-level (separate concern — different SUT entry point).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bowhead_core.search_config import SearchModelRegistry, SearchModelSpec


def _search_resolver():
    """fake team_search_resolver —— 团队配了一个 dashscope_compat web_search 模型."""
    spec = SearchModelSpec(
        name="qwen-plus-search",
        capability="web_search",
        provider="aliyun",
        real_id="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-test",
        timeout_sec=25,
        protocol="dashscope_compat",
        ssrf_guard=True,
    )
    reg = SearchModelRegistry(by_name={spec.name: spec}, active={"web_search": spec.name})

    async def _resolve(team_id: str) -> SearchModelRegistry | None:
        return reg

    return _resolve


@pytest.mark.asyncio
async def test_lead_can_invoke_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """role='lead' MUST pass the registry role gate for web_search.

    Pre-fix: registry would short-circuit with `invocation.error =
    'ToolPermissionError: role=lead cannot call tool=web_search ...'` because
    `allowed_for_roles = frozenset({'analyst'})`. Post-fix: yaml lists
    `[analyst, lead]` → codegen → `frozenset({'analyst', 'lead'})` → call
    flows through to the handler (which we mock).
    """
    # 1) yaml-driven role binding source of truth.
    from bowhead_core.agents._generated.role_bindings import (
        _PARADIGM_V2_ROLE_MAP,
    )
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
    from bowhead_core.web_search import register_web_search_tool

    assert "lead" in _PARADIGM_V2_ROLE_MAP["web_search"], (
        "yaml→codegen contract broken: web_search.allowed_for_roles missing 'lead'. Regen via `uv run python -m bowhead_cli.codegen_tool_aliases`."
    )
    assert "analyst" in _PARADIGM_V2_ROLE_MAP["web_search"]

    # 2) Runtime: registry execute() with role='lead' must NOT raise / set
    # ToolPermissionError. Mock dashscope HTTP layer so the test is hermetic.
    monkeypatch.setenv("BOWHEAD_DASHSCOPE_API_KEY", "sk-test-key")
    reg = ToolRegistry()
    assert register_web_search_tool(reg, team_search_resolver=_search_resolver()) is True

    # Apply paradigm v2 role bindings (gateway main.py path).
    from bowhead_core.tool_registry import apply_paradigm_v2_role_bindings

    apply_paradigm_v2_role_bindings(reg)
    spec = reg.get("web_search")
    assert "lead" in spec.allowed_for_roles, spec.allowed_for_roles

    # Mock httpx2 so handler doesn't hit network.
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [
                {
                    "message": {
                        "content": "Mock summary [1]",
                        "citations": [
                            {"title": "T1", "url": "http://u1", "snippet": "s1"},
                        ],
                    },
                }
            ],
        },
        text="",
    )

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        post = AsyncMock(return_value=fake_resp)

    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m_test",
        user_id="u_test",
        turn_id="t_test",
        datarc=None,  # type: ignore[arg-type]  # not used by web_search handler
        role="lead",  # ← THE key under test
        team_id="team_1",
    )

    with patch("httpx2.AsyncClient", _FakeClient):
        invocation = await reg.execute(
            "web_search",
            {"query": "nvidia 2026 q1 revenue"},
            ctx,
        )

    # Pre-fix expected: invocation.error startswith "ToolPermissionError".
    # Post-fix expected: error is None, result populated.
    assert invocation.error is None or "ToolPermissionError" not in (invocation.error or ""), (
        f"role=lead rejected at registry gate: {invocation.error!r}"
    )
    # And the mocked handler actually ran.
    assert invocation.result is not None, "handler did not execute"


@pytest.mark.asyncio
async def test_analyst_can_still_invoke_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: adding 'lead' must not break 'analyst' path."""
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        ToolRegistry,
        apply_paradigm_v2_role_bindings,
    )
    from bowhead_core.web_search import register_web_search_tool

    monkeypatch.setenv("BOWHEAD_DASHSCOPE_API_KEY", "sk-test")
    reg = ToolRegistry()
    register_web_search_tool(reg, team_search_resolver=_search_resolver())
    apply_paradigm_v2_role_bindings(reg)

    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok", "citations": []}}]},
        text="",
    )

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        post = AsyncMock(return_value=fake_resp)

    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        role="analyst",
        team_id="team_1",
    )
    with patch("httpx2.AsyncClient", _FakeClient):
        inv = await reg.execute("web_search", {"query": "q"}, ctx)
    assert inv.error is None or "ToolPermissionError" not in (inv.error or "")


@pytest.mark.asyncio
async def test_composer_role_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative regression: roles NOT in allowed_roles still get rejected.

    If someone widens allowed_roles too aggressively this test breaks.
    """
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        ToolRegistry,
        apply_paradigm_v2_role_bindings,
    )
    from bowhead_core.web_search import register_web_search_tool

    monkeypatch.setenv("BOWHEAD_DASHSCOPE_API_KEY", "sk-test")
    reg = ToolRegistry()
    register_web_search_tool(reg, team_search_resolver=_search_resolver())
    apply_paradigm_v2_role_bindings(reg)

    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        role="composer",  # not in [analyst, lead]
        team_id="team_1",
    )
    inv = await reg.execute("web_search", {"query": "q"}, ctx)
    assert inv.error is not None and "ToolPermissionError" in inv.error
