"""Populate peer capability metadata through the actual roster-gathering path.

Rendering tests cannot detect an omitted can_fetch_bi field that silently remains
False. Custom peers with data tools must be eligible to replace a disabled builtin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import anyio
import pytest
from bowhead_core.agents.bi_capability import profile_can_fetch_bi
from bowhead_core.agents.lead_orchestrator import _gather_lead_peers


@dataclass
class _Row:
    """team_subagent_store 行的最小形状（`list_visible_team_subagents` 读这些字段）。"""

    name: str
    description: str
    tool_refs: tuple[str, ...] = ()
    id: str = "osa_x"
    visibility: str = "team_wide"
    created_by: str | None = None
    system_prompt: str = "你是一个分析 agent。"


@dataclass
class _Store:
    rows: list[_Row] = field(default_factory=list)
    # None ⇒ `list_visible_team_subagents` uses pre-cutover compatibility (test adapter).
    db_path: None = None

    async def list_team_subagents(self, team_id: str, *, only_enabled: bool = True) -> list[_Row]:
        return self.rows


def _gather(store: _Store) -> list:
    return anyio.run(
        lambda: _gather_lead_peers(
            registry=None,  # Test only team branch; _profile_can_fetch_bi directly tests builtins
            workspace_id="ws_1",
            custom_store=None,
            user_id="usr_1",
            team_id="team_fixture",
            team_subagent_store=store,
        )
    )


# ─────────── Builtins ───────────


def test_builtin_bi_capability_reads_real_profiles() -> None:
    """判定读的是真 PROFILES，不是名字白名单。

    ⚠ **只断言 compute 与不存在的名字**。别在这里断言"某个内置没有 BI 工具"：
    生产形态（`register_all_system_manifests` 覆盖 PROFILES 之后）几乎每个内置
    都带 `bi_query`，而**未 bootstrap 的 fallback dict 里只有 compute** ——
    照 fallback 写断言会在单跑时绿、全量跑时红（前面的测试一 bootstrap 就变了），
    而且绿的那次是在对着一个生产里不存在的状态做保证。
    """
    assert profile_can_fetch_bi("compute") is True
    assert profile_can_fetch_bi("no-such-peer") is False


# ─────────── Team ───────────


def test_team_peer_with_bi_query_is_marked_capable() -> None:
    """A custom peer with bi_query can hold the fetch role."""
    peers = _gather(_Store([_Row("regional-data-analyst", "Regional Sales Analyst", ("bi_query", "python_sandbox"))]))
    hit = next(p for p in peers if p.name == "regional-data-analyst")
    assert hit.can_fetch_bi is True
    assert hit.source == "team"


def test_team_peer_without_bi_tools_is_not_marked() -> None:
    """没有取数工具就不许冒充取数 peer——派给它只会让它编。"""
    peers = _gather(_Store([_Row("doc-writer", "写文档", ("kb_search", "python_sandbox"))]))
    assert next(p for p in peers if p.name == "doc-writer").can_fetch_bi is False


def test_teammcp_tools_do_not_count_as_bi_capability() -> None:
    """`list_visible_team_subagents` 会剔掉 `teammcp.*`；一个只有外部工具的 agent
    不该因为名字里带 bi 就被当成取数 peer。"""
    peers = _gather(_Store([_Row("ext", "外部系统", ("teammcp.foo.bi_query",))]))
    assert next(p for p in peers if p.name == "ext").can_fetch_bi is False


def test_legacy_bi_tool_names_still_count() -> None:
    """老白名单里是 bi_fetch_rows / bi_ask_question 的 agent 同样算取数 peer
    （`BI_FETCH_TOOL_NAMES` 三个名字都要认，漏一个就是静默降级）。"""
    for tool in ("bi_ask_question", "bi_fetch_rows"):
        peers = _gather(_Store([_Row("legacy", "老 agent", (tool,))]))
        assert next(p for p in peers if p.name == "legacy").can_fetch_bi is True, tool


# ── WP M3-12 batch 2: gate image generation by contract, not literal name ──


def test_missing_image_service_retains_agents_and_other_responsibilities() -> None:
    """A service prerequisite must not hide supported edits, exports or analysis."""
    from bowhead_core.agents.capability_roles import IMAGE_GEN_CONTRACTS, manifest_primary_capability
    from bowhead_core.agents.prompts.peer_block import build_prompt_roster, render_peer_block
    from bowhead_core.agents.registry import AgentRegistry

    registry = AgentRegistry.bootstrap()

    async def gather(unconfigured: frozenset[str], disabled: bool = False):
        from types import SimpleNamespace

        return await _gather_lead_peers(
            registry=registry,
            workspace_id="ws_x",
            custom_store=None,
            user_id="u_1",
            unconfigured_capability_tools=unconfigured,
            builtin_overrides={name: SimpleNamespace(enabled=False) for name in image_names} if disabled else None,
        )

    image_names = {name for name in registry.list_all() if manifest_primary_capability(registry.get(name)) in IMAGE_GEN_CONTRACTS}
    assert image_names
    configured = anyio.run(gather, frozenset())
    limited = anyio.run(gather, frozenset({"image_generate"}))
    assert {peer.name for peer in configured} == {peer.name for peer in limited}
    for peer in limited:
        if peer.name in image_names:
            assert "image_generate" not in peer.tool_names
            assert "image_generate" in peer.unavailable_tools
            assert "canvas_update_design_text" in peer.tool_names
            assert "canvas_flatten_design" in peer.tool_names
    assert not image_names & {peer.name for peer in anyio.run(gather, frozenset({"image_generate"}), True)}
    assert build_prompt_roster(configured).has("image_gen")
    assert not build_prompt_roster(limited).has("image_gen")
    prompt = render_peer_block(limited)
    assert "Team image generation is not configured" in prompt
    assert "remain callable" in prompt
    assert "existing background image" in prompt
    assert "Do not ask the user to choose internal Agents" in prompt
    assert "<agent_service_limits>" not in render_peer_block(configured)


def test_team_image_role_tracks_current_connection_readiness() -> None:
    from bowhead_core.agents.prompts.peer_block import build_prompt_roster

    store = _Store([_Row("team-designer", "Generate requested images", ("image_generate",))])
    ready = _gather(store)
    assert build_prompt_roster(ready).holders["image_gen"] == "team-designer"
    unconfigured = anyio.run(
        lambda: _gather_lead_peers(
            registry=None,
            workspace_id="ws_1",
            custom_store=None,
            user_id="usr_1",
            team_id="team_fixture",
            team_subagent_store=store,
            unconfigured_capability_tools=frozenset({"image_generate"}),
        )
    )
    assert build_prompt_roster(unconfigured).holders["image_gen"] is None


@pytest.mark.parametrize("source", ["system", "team", "user"])
@pytest.mark.parametrize("query_tool", ["bi_query", "teammcp.ibt_test.pbi_execute_dax"])
def test_unavailable_query_tools_cannot_restore_the_data_role(source, query_tool) -> None:
    from bowhead_core.agents.prompts.peer_block import build_prompt_roster

    name = "source-reader"

    class Registry:
        def get(self, profile, workspace_id):
            if profile == "lead":
                return SimpleNamespace(callable_peers=[name])
            return SimpleNamespace(
                description="Read the configured source", tools=SimpleNamespace(allow=(query_tool,)), primary_capability="cap_data_query"
            )

    class CustomStore:
        async def list_for_user(self, user_id, *, status):
            return [SimpleNamespace(name=name, description="Read the configured source", allowed_tools=(query_tool,))]

    async def gather(unavailable):
        return await _gather_lead_peers(
            registry=Registry() if source == "system" else None,
            workspace_id="ws_1",
            custom_store=CustomStore() if source == "user" else None,
            user_id="usr_1",
            team_id="team_fixture",
            team_subagent_store=_Store([_Row(name, "Read the configured source", (query_tool,))]) if source == "team" else None,
            unconfigured_capability_tools=unavailable,
        )

    ready = anyio.run(gather, frozenset())
    assert build_prompt_roster(ready).holder("data") == name
    unavailable = anyio.run(gather, frozenset({query_tool}))
    assert len(unavailable) == 1
    assert unavailable[0].tool_names == frozenset()
    assert unavailable[0].can_fetch_bi is False
    assert build_prompt_roster(unavailable).holder("data") is None
