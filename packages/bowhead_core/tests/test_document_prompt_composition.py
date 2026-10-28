"""Document method guidance follows the actual Lead Tool advertisement."""

from dataclasses import replace

import pytest
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import default_prompt_peers
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.documents.tool import register_document_tool
from bowhead_core.documents.turn_policy import NATIVE_DOCUMENT_GUIDANCE
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from test_subagent import _router

from bowhead_router import LLMResponse


@pytest.mark.integration
@pytest.mark.parametrize("revision", [False, True])
@pytest.mark.parametrize("deck_peer,compute_peer", [(True, True), (False, True), (True, False), (False, False)])
@pytest.mark.parametrize("availability", ["available", "absent", "whitelist", "hidden", "role", "denied", "unconfigured", "overlay"])
async def test_composed_lead_document_guidance_matches_advertised_tools(monkeypatch, availability, deck_peer, compute_peer, revision):
    registry = ToolRegistry()
    if availability != "absent":
        register_document_tool(registry, service=lambda: None)
    if availability in {"hidden", "role"}:
        spec = registry.get("template_document_submit")
        registry = registry.clone_with_spec_override(
            replace(spec, **({"visible_to_llm": False} if availability == "hidden" else {"allowed_for_roles": frozenset({"analyst"})}))
        )
    context = ToolExecutionContext(
        user_id="u1", member_uid="u1", workspace_id="ws_test", turn_id="t_prompt", session_id="s_prompt", role="lead", datarc=None
    )
    if revision:
        # Only exercise presence-based prompt assembly; this object grants no domain authority.
        context.document_revision_target = object()
    if availability == "overlay":
        context.tool_view = registry.clone_with_spec_override(replace(registry.get("template_document_submit"), visible_to_llm=False))
    if availability == "unconfigured":
        context.unconfigured_capability_tools = frozenset({"template_document_submit"})
    profile = replace(PROFILES["lead"], tool_whitelist=() if availability == "whitelist" else ("template_document_submit",))
    monkeypatch.setitem(PROFILES, "lead", profile)
    peers = [peer for peer in default_prompt_peers() if (deck_peer or peer.name != "consultant") and (compute_peer or peer.name != "compute")]
    prompt = build_lead_system_prompt("ws_test", facts=[], peers=peers, skill_catalog_block_override="")
    router = _router(LLMResponse(content="Ready.", finish_reason="stop"))
    adapter = router._registry._adapters["p1"]
    requests = []
    invoke = adapter.invoke

    async def capture(request):
        requests.append(request)
        return await invoke(request)

    monkeypatch.setattr(adapter, "invoke", capture)
    run = SubagentRunner(router, registry).run(
        profile_name="lead",
        task="Describe the available presentation workflows without creating a file.",
        parent_ctx=context,
        parent_bus=TurnEventBus(),
        parent_turn_id=context.turn_id,
        extra_instructions=prompt,
        tool_deny=frozenset({"template_document_submit"}) if availability == "denied" else frozenset(),
    )
    if revision and availability != "available":
        with pytest.raises(ValueError, match="document_revision_unavailable"):
            await run
        assert requests == []
        return
    await run
    assert len(requests) == 1
    request = requests[0]
    available = "template_document_submit" in {tool.name for tool in request.tools}
    assert available == (availability == "available")
    assert request.system.count(NATIVE_DOCUMENT_GUIDANCE) == int(available)
    assert ("This request explicitly revises the exact saved document draft" in request.system) == revision
    assert ("Uploaded-template updates are not available in this task" in request.system) == (not available)
    assert "reference-style new presentations" in request.system
    assert "source-derived PDF" in request.system
    assert "ordinary PDF / Excel (xlsx) / Word (docx)" in request.system
    assert "produces a downloadable deck from the template" not in request.system
    assert "exports are rendered\nfrom the canvas" not in request.system
    if not deck_peer:
        assert "New PPT slide-deck post currently has no holder" in request.system
        assert "consultant" not in request.system
    if not compute_peer:
        assert "ordinary PDF / Excel (xlsx) / Word (docx) export post currently has no holder" in request.system
        assert "This does not determine availability of a template draft's source-derived PDF" in request.system
        assert "compute" not in request.system


@pytest.mark.integration
@pytest.mark.parametrize("profile_name", ["compute", "consultant"])
async def test_inspection_peer_does_not_receive_lead_document_instructions(monkeypatch, profile_name):
    manifest = AgentRegistry.bootstrap().get(profile_name)
    monkeypatch.setitem(PROFILES, profile_name, manifest_to_profile(manifest))
    registry = ToolRegistry()
    register_document_tool(registry, service=lambda: None)
    router = _router(LLMResponse(content="Read-only observations.", finish_reason="stop"))
    adapter = router._registry._adapters["p1"]
    invoke = adapter.invoke
    requests = []

    async def capture(request):
        requests.append(request)
        assert NATIVE_DOCUMENT_GUIDANCE not in request.system
        assert "Uploaded-template updates are not available in this task" not in request.system
        return await invoke(request)

    monkeypatch.setattr(adapter, "invoke", capture)
    context = ToolExecutionContext(user_id="u1", member_uid="u1", workspace_id="ws_test", turn_id="t_peer", session_id="s_peer", datarc=None)
    result = await SubagentRunner(router, registry).run(
        profile_name=profile_name,
        task="Return read-only observations.",
        parent_ctx=context,
        parent_bus=TurnEventBus(),
        parent_turn_id=context.turn_id,
    )
    assert requests, result
