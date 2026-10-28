"""Keep Lead routing valid for data sources other than Datarc."""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import PeerEntry
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import LLMResponse, ToolSchema
from test_subagent_tool_guidance import _context, _ScriptedRouter

pytestmark = pytest.mark.unit


def _block(prompt: str, tag: str) -> str:
    return prompt.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]


def _peers(*, data: bool, computation: bool) -> list[PeerEntry]:
    peers = []
    if data:
        peers.append(
            PeerEntry(
                name="warehouse-reader",
                description="Query the selected Power BI model.",
                source="team",
                tool_names=frozenset({"teammcp.ibt_sales.pbi_execute_dax"}),
                primary_capability="cap_data_query",
            )
        )
    if computation:
        peers.append(
            PeerEntry(
                name="calculator",
                description="Calculate using supplied data.",
                source="team",
                tool_names=frozenset({"python_exec"}),
                primary_capability="cap_compute",
            )
        )
    return peers


@pytest.mark.parametrize("data", [False, True])
@pytest.mark.parametrize("computation", [False, True])
async def test_model_receives_independent_data_and_computation_routes(monkeypatch: pytest.MonkeyPatch, data: bool, computation: bool) -> None:
    async def spawn(_args: dict, _ctx: ToolExecutionContext) -> dict:
        raise AssertionError("The prompt inspection must not dispatch a helper.")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(schema=ToolSchema(name="subagent_spawn", description="Delegate a task.", input_schema={"type": "object"}), handler=spawn)
    )
    profile = SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=("subagent_spawn",), system_prompt="", max_iterations=2)
    monkeypatch.setitem(PROFILES, "lead", profile)
    prompt = build_lead_system_prompt(
        "ws_guidance", facts=[], peers=_peers(data=data, computation=computation), has_datarc=False, skill_catalog_block_override=""
    )
    router = _ScriptedRouter(LLMResponse(content="Ready."))
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="lead",
        task="Explain the available routes.",
        parent_turn_id="t_guidance",
        parent_bus=TurnEventBus(),
        parent_ctx=_context(),
        extra_instructions=prompt,
    )

    assert result.ok, result.error
    request = router.requests[0]
    assert [tool.name for tool in request.tools] == ["subagent_spawn"]
    decision = _block(request.system, "query_classification_decision_tree")
    data_line = next(line for line in decision.splitlines() if line.startswith("- contains metric/time/dim"))
    code_line = next(line for line in decision.splitlines() if line.startswith("- **Run code / pure calculation"))
    assert ("MUST spawn warehouse-reader" in data_line) is data
    assert ("MUST spawn calculator" in code_line) is computation
    assert "warehouse-reader" not in code_line
    if not computation:
        assert "no Agent here can run code" in code_line
    if computation:
        assert "go straight to Step 2 via calculator" in decision
    if data:
        parallel = _block(request.system, "parallel_tool_calls")
        assert "warehouse-reader" in parallel and "available Tools" in parallel
        assert "batches bi_query" not in parallel
    metadata = _block(request.system, "workspace_context_lazy")
    assert "actual data source" in metadata and "available" in metadata
    assert "bi_schema()" not in metadata
    notice = _block(request.system, "no_data_source")
    assert "applies only to Datarc" in notice
    assert "other configured data sources" in notice


@pytest.mark.parametrize(
    ("reason", "remedy"),
    [
        ("no_connection", "team admin"),
        ("no_identity", "enable their Datarc account first"),
        ("zero_workspaces", "add them to the relevant Datarc workspace"),
        ("transient", "try again shortly"),
    ],
)
def test_datarc_failure_preserves_its_remedy_without_disabling_other_sources(reason: str, remedy: str) -> None:
    prompt = build_lead_system_prompt(
        "ws_guidance",
        facts=[],
        peers=_peers(data=True, computation=True),
        has_datarc=False,
        no_datarc_reason=reason,
        skill_catalog_block_override="",
    )
    notice = _block(prompt, "no_data_source")
    assert remedy in notice
    assert "applies only to Datarc" in notice
    assert "preserve any source the user explicitly required" in notice
    assert "do not promise charts / metrics" not in notice


def test_datarc_workspace_operations_keep_their_source_and_availability_boundary() -> None:
    prompt = build_lead_system_prompt("ws_guidance", facts=[], has_datarc=True, skill_catalog_block_override="")
    scope = _block(prompt, "datarc_workspace_scope")
    assert "only to requests using Datarc" in scope
    assert "only when it is available" in scope
    assert "tool_list_workspaces" in scope and "tool_switch_context" in scope
    assert "Do not switch on your own" in scope
    bail = _block(prompt, "peer_bail_handling")
    assert "actual source" in bail
    assert "bi_concept_resolve" not in bail
