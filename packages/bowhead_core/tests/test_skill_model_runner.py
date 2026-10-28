"""Real retained composition -> existing runner -> router -> scripted adapter.

This exercises model-input plumbing only. It does not mount the mixed scheduler,
perform resource tools, write injection evidence or measure real model quality.
"""

from __future__ import annotations

import copy

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_read_model_input import RequiredSkillModelInput
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.router import AgentBinding, Router
from test_skill_read_model_input import Adapter, Registry
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case

claimed_case = _claimed_case
receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.mark.parametrize("window", [1000000, 2000, None])
async def test_real_loop_keeps_full_saved_methods_or_calls_no_adapter(claimed_case, window):
    case = claimed_case
    claim = await claim_reference_skill_primary_for_worker(**case.args)
    composition, prepared, rendered = await restore(case)
    required = RequiredSkillModelInput.from_rendered(rendered)
    adapter = Adapter(["盈利分析已完成。", "普通任务完成。"])
    router = Router(Registry(primary=adapter))
    for agent_type in ("analyst", "echo"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("primary", "test-model")))
    if window is not None:
        router.set_context_windows({("primary", "test-model"): window})
    ctx = ToolExecutionContext(**case.scope.model_dump(), turn_id="t_reference", member_uid=case.fixture.case.fixture.member_id, datarc=None)
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), team_subagent_store=case.fixture.case.fixture.users)
    before = claim.primary.binding.definition.copy()
    result = await runner.run(
        profile_name=claim.primary.binding.runtime_profile_name,
        task=claim.primary.source.input_document.text_content(),
        parent_turn_id="t_reference",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
        exact_binding=claim.primary.binding,
        required_model_input=required,
    )
    if window != 1000000:
        assert not result.ok and adapter.calls == []
        return
    assert result.ok and len(adapter.calls) == 1
    sent = adapter.calls[0]
    assert sent.input_admission is required
    assert sum(message.content == required.content for message in sent.messages) == 1
    assert rendered.content.decode() in sent.messages[0].content
    assert claim.primary.source.input_document.text_content() in [message.content for message in sent.messages]
    assert "SAVED_METHOD_LABEL" not in sent.system
    assert claim.primary.binding.definition == before
    assert composition.rendered_content_hash == rendered.rendered_content_hash
    # A second independent call on the shared runner, with a shallow ctx copy,
    # receives no per-run input policy/material. This is not a second claim.
    other = await runner.run(
        profile_name="compute", task="普通任务", parent_turn_id="t_reference", parent_bus=TurnEventBus(), parent_ctx=copy.copy(ctx)
    )
    assert other.ok and adapter.calls[-1].input_admission is None
    assert all(required.content not in message.content for message in adapter.calls[-1].messages)
