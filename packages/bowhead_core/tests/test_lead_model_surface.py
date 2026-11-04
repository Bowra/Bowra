"""Observe Lead instructions and provider schemas through the original runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agents import lead_orchestrator as lead
from bowhead_core.agents.prompts.peer_block import default_prompt_peers
from bowhead_core.agents.turn_context import SerializableTurnContext
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, ToolSchema
from test_subagent_resume_runtime import _EmptyCatalog, _FakeBus
from test_subagent_resume_store import _db

pytestmark = pytest.mark.integration


class Recorder:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.modes: list[str] = []

    async def invoke(self, _agent_type: str, request: LLMRequest, **_kwargs: object) -> LLMResponse:
        self.requests.append(request)
        self.modes.append("invoke")
        return LLMResponse(content="Finished.")

    async def stream(self, _agent_type: str, request: LLMRequest, **_kwargs: object) -> AsyncIterator[LLMChunk]:
        self.requests.append(request)
        self.modes.append("stream")
        yield LLMChunk(delta="Finished.")
        yield LLMChunk(finish_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["invoke", "stream"])
@pytest.mark.parametrize("search", [False, True], ids=["unconfigured-search", "configured-search"])
@pytest.mark.parametrize("kb_mode", ["specialist", "absent", "direct"])
async def test_lead_request_matches_admitted_search_and_knowledge_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool, search: bool, kb_mode: str
) -> None:
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    registry = ToolRegistry()
    for name in ("web_search", "web_fetch", "kb_search"):
        registry.register(
            ToolSpec(schema=ToolSchema(name=name, description="Read supplied evidence", input_schema={"type": "object"}), handler=AsyncMock())
        )
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead", llm_agent_type="lead", tool_whitelist=("web_search", "web_fetch", "kb_search"), system_prompt="", max_iterations=2
        ),
    )
    peers = [p for p in default_prompt_peers() if kb_mode != "absent" or p.name != "kb-analyst"]
    path = await _db(tmp_path)
    from bowhead_core.session_store import SessionStore

    ctx = ToolExecutionContext(
        workspace_id="ws_1",
        member_uid="member",
        user_id="u_1",
        turn_id="t_2",
        session_id="s_1",
        team_id="team_1",
        datarc=None,
        session_store=SessionStore(path),
    )
    ctx._actor_scoped_prompt_peers = tuple(peers)
    ctx.skill_catalog = _EmptyCatalog()
    ctx.knowledge_direct_use = kb_mode == "direct"
    ctx.unconfigured_capability_tools = frozenset() if search else frozenset({"web_search"})
    router = Recorder()
    runner = SubagentRunner(router=router, tool_registry=registry)
    runtime = lead.LeadRuntime(router=router, tool_registry=registry, parent_bus=_FakeBus(), parent_ctx=ctx, subagent_runner=runner)
    # Capture the production handoff and use the real runner for its request assembly.
    original = runner.run

    # Preserve the original runner's heterogeneous admission keyword arguments.
    async def run(**kwargs: Any) -> SubagentResult:
        kwargs.pop("stream_callback", None)
        if stream:

            async def on_text(*_args: object, **_kwargs: object) -> None:
                return None

            kwargs["stream_callback"] = on_text
        return await original(**kwargs)

    monkeypatch.setattr(runner, "run", run)
    user_text = 'Explain this quoted example: "web_search and kb_search".'
    scope = SerializableTurnContext(user_id="u_1", workspace_id="ws_1", session_id="s_1", turn_id="t_2", role="user")
    if kb_mode == "direct":
        from bowhead_core.agents.prompts.lead import build_lead_system_prompt

        prompt = build_lead_system_prompt(
            "ws_1", facts=[], peers=peers, knowledge_direct_use=True, unconfigured_capability_tools=ctx.unconfigured_capability_tools
        )
        await runner.run(
            profile_name="lead", task=user_text, parent_turn_id="t_2", parent_bus=runtime.parent_bus, parent_ctx=ctx, extra_instructions=prompt
        )
    else:
        await lead._lead_loop(scope, user_text, runtime)
    assert router.requests
    request = router.requests[0]
    assert router.modes[0] == ("stream" if stream else "invoke")
    names = {tool.name for tool in request.tools}
    assert ("web_search" in names) is search
    assert "web_fetch" in names
    assert ("kb_search" in names) is (kb_mode != "specialist")
    assert ("→ use `web_search`" in request.system) is search
    assert ("External search is not configured" in request.system) is (not search)
    assert ("several independent kb_search:" in request.system) is (kb_mode != "specialist")
    assert (" / use kb_search)" in request.system) is (kb_mode != "specialist")
    assert "</lead_orchestrator>" in request.system
    assert any(user_text in str(message.content) for message in request.messages)
