"""User-selected Skills in Lead's conversation and in dispatches (slices S3 and S4).

docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.context_hygiene import MESSAGE_KIND_POLICY
from bowhead_core.context_view import eligible_context_range
from bowhead_core.selected_skill_input import (
    SKILL_CONTENT_MESSAGE_NAME,
    SelectedSkill,
    SelectedSkillUnavailable,
    dispatch_skill_input,
    history_skill_texts,
    load_selected_skills,
    selected_skill_messages,
)
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_model_input import RequiredSkillModelInput
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.types import Message, ToolCall

from bowhead_core import ordinary_lead_material

pytestmark = pytest.mark.unit
SKILL = SkillResourceRef(kind="skill", source_kind="user", id="sk_c029829564ae", version="sha256:" + "b" * 64)
TEXT = '<skill_content name="business-review">\n# Skill: business-review\n\n## 执行步骤\n- 营业额\n</skill_content>'
SELECTED = SelectedSkill(resource=SKILL, name="business-review", text=TEXT)


def test_a_new_selection_carries_guidance_and_the_readable_method():
    [message] = selected_skill_messages([SELECTED], history=[Message(role="user", content="earlier")])
    assert message.role == "user" and message.name == SKILL_CONTENT_MESSAGE_NAME
    assert message.content.startswith('The user selected the Skill "business-review" for the request above.')
    assert 'skills=["business-review"]' in message.content and "Do not restate, narrow or widen" in message.content
    assert message.content.endswith(TEXT)


def test_the_same_version_already_in_history_becomes_a_short_note():
    earlier = selected_skill_messages([SELECTED], history=[])[0]
    [message] = selected_skill_messages([SELECTED], history=[earlier])
    assert TEXT not in message.content
    assert "unchanged from the copy loaded earlier" in message.content and 'skills=["business-review"]' in message.content
    changed = SelectedSkill(resource=SKILL, name="business-review", text=TEXT.replace("营业额", "利润"))
    assert selected_skill_messages([changed], history=[earlier])[0].content.endswith(changed.text)


def test_skill_messages_stay_in_the_published_conversation():
    assert MESSAGE_KIND_POLICY[SKILL_CONTENT_MESSAGE_NAME] == "conversation"


def test_compaction_keeps_the_selection_with_its_request_and_the_work_after_it_eligible():
    call = ToolCall(id="c1", name="bi_query", arguments={})
    messages = [
        Message(role="user", content="old request " + "x" * 50),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="request"),
        Message(role="user", content="skill", name=SKILL_CONTENT_MESSAGE_NAME),
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", content="r" * 4000, tool_call_id="c1", name="bi_query"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c2", name="bi_query", arguments={})]),
        Message(role="tool", content="r", tool_call_id="c2", name="bi_query"),
    ]
    start, end = eligible_context_range(messages, current_request=2, canonical_count=len(messages))
    assert (start, end) == (4, 6)


class _Store:
    def __init__(self, skills):
        self.skills = skills

    async def get_chat_turn_user_source(self, *, turn_id, user_id, workspace_id):
        return ChatTurnUserSource(
            turn_id=turn_id,
            session_id="s_1",
            user_id=user_id,
            workspace_id=workspace_id,
            user_text="/business-review 分析",
            attachment_ids=(),
            attachment_refs=(),
            parts=None,
            selected_skills=self.skills,
        )


def _ctx(skills) -> SimpleNamespace:
    return SimpleNamespace(session_store=_Store(skills), user_id="u_1", workspace_id="ws_1", team_id="team_1", session_id="s_1")


@pytest.mark.asyncio
async def test_load_prepares_each_skill_again_and_renders_it(monkeypatch):
    material = {
        "schema_version": 1,
        "read_only_entry": {"entry_file": "SKILL.md"},
        "files": [{"path": "SKILL.md", "text": "## 执行步骤\n- 营业额\n", "sha256": "x"}, {"path": "refs/a.md", "text": "A", "sha256": "y"}],
    }
    calls = []

    class Sources:
        async def prepare(self, resource, *, scope):
            calls.append((resource, scope.session_id))
            return SimpleNamespace(proof=SimpleNamespace(name="business-review"), material=SimpleNamespace(content=json.dumps(material).encode()))

    monkeypatch.setattr(ordinary_lead_material, "_sources", lambda ctx, resource, name: Sources())
    [selected] = await load_selected_skills(_ctx((SKILL,)), turn_id="t_1")
    assert calls == [(SKILL, "s_1")]
    assert selected.name == "business-review" and "## 执行步骤\n- 营业额" in selected.text and '<skill_file path="refs/a.md">' in selected.text
    assert "sha256" not in selected.text


@pytest.mark.asyncio
async def test_load_refuses_a_skill_that_is_no_longer_readable(monkeypatch):

    class Sources:
        async def prepare(self, resource, *, scope):
            raise SkillReadMaterialError("reference_skill_version_changed")

    monkeypatch.setattr(ordinary_lead_material, "_sources", lambda ctx, resource, name: Sources())
    with pytest.raises(SelectedSkillUnavailable, match="reference_skill_version_changed"):
        await load_selected_skills(_ctx((SKILL,)), turn_id="t_1")


@pytest.mark.asyncio
async def test_turns_without_a_selection_load_nothing(monkeypatch):
    assert await load_selected_skills(_ctx(None), turn_id="t_1") == ()
    assert await load_selected_skills(SimpleNamespace(session_store=object(), user_id="u", workspace_id="w"), turn_id="t_1") == ()
    assert await load_selected_skills(SimpleNamespace(session_store=_Store((SKILL,))), turn_id="t_1") == ()


@pytest.mark.asyncio
async def test_a_failed_source_read_runs_the_turn_without_a_selection():
    class Store:
        async def get_chat_turn_user_source(self, **kwargs):
            raise OSError("database is locked")

    assert await load_selected_skills(SimpleNamespace(session_store=Store(), user_id="u", workspace_id="w"), turn_id="t_1") == ()


def test_dispatch_accepts_only_selected_names():
    ctx = SimpleNamespace(_selected_skills=(SELECTED,))
    required, refusal = dispatch_skill_input(ctx, ["business-review"])
    assert refusal is None and isinstance(required, RequiredSkillModelInput)
    assert TEXT in required.content and required.content.startswith("The user selected the method below for this task.")
    for value, hint in ((["other"], "business-review"), ([], "list"), ("business-review", "list")):
        assert dispatch_skill_input(ctx, value)[1]["path"] == "skills"
        assert hint in dispatch_skill_input(ctx, value)[1]["error"]
    assert "No Skill selected with / is in this conversation" in dispatch_skill_input(SimpleNamespace(), ["business-review"])[1]["error"]


def test_a_later_turn_dispatches_a_skill_selected_earlier_from_leads_history():
    earlier = selected_skill_messages([SELECTED], history=[])[0]
    history = [earlier, Message(role="assistant", content="done")]
    assert history_skill_texts(history) == {"business-review": TEXT}
    ctx = SimpleNamespace(_selected_skills=(), _history_skill_texts=history_skill_texts(history))
    required, refusal = dispatch_skill_input(ctx, ["business-review"])
    assert refusal is None and required.content.endswith(TEXT)
    # A fresh selection in this request wins over the older copy.
    newer = SelectedSkill(resource=SKILL, name="business-review", text=TEXT.replace("营业额", "利润"))
    ctx = SimpleNamespace(_selected_skills=(newer,), _history_skill_texts=history_skill_texts(history))
    assert dispatch_skill_input(ctx, ["business-review"])[0].content.endswith(newer.text)
    # A note carries no copy, and a summarized-away copy is not dispatchable.
    note = selected_skill_messages([SELECTED], history=[earlier])[0]
    assert history_skill_texts([note]) == {}


class _Runner:
    def __init__(self, *, admit: bool) -> None:
        self.kwargs: dict = {}
        if admit:
            self.admit_execution_binding = self._admit
            self.team_subagent_store = SimpleNamespace(db_path=":memory:")

    async def _admit(self, **kwargs):
        return SimpleNamespace(execution_id="aex_test", agent_id="system:compute")

    async def run(self, **kwargs):
        self.kwargs = kwargs
        return SubagentResult(profile="compute", subagent_id="sub_test", ok=True, final_answer="done")


class _Bus:
    async def emit(self, event) -> None:  # noqa: ARG002
        return None


class _Lifecycle:
    def __init__(self, db_path) -> None:  # noqa: ARG002
        return None

    async def mark_running(self, *args, **kwargs) -> None:
        return None

    async def mark_terminal(self, *args, **kwargs) -> None:
        return None


async def _spawn(monkeypatch, runner, args):
    monkeypatch.setattr("bowhead_core.agent_execution_binding.AgentExecutionBindingStore", _Lifecycle)
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(workspace_id="1", member_uid="m", user_id="u", turn_id="t", datarc=None, subagent_runner=runner, parent_bus=_Bus())
    ctx._selected_skills = (SELECTED,)
    return await registry.execute("subagent_spawn", {"profile": "compute", "task": "分析2026年1-10月的经营情况", **args}, ctx)


@pytest.mark.asyncio
async def test_spawn_without_a_binding_refuses_skills_before_running(monkeypatch):
    runner = _Runner(admit=False)
    invocation = await _spawn(monkeypatch, runner, {"skills": ["business-review"]})
    assert invocation.result["path"] == "skills" and runner.kwargs == {}


@pytest.mark.asyncio
async def test_spawn_refuses_an_unselected_skill(monkeypatch):
    runner = _Runner(admit=False)
    invocation = await _spawn(monkeypatch, runner, {"skills": ["other"]})
    assert invocation.result["path"] == "skills" and runner.kwargs == {}


def test_spawn_advertises_selected_skills():
    registry = build_default_registry()
    register_subagent_tool(registry)
    [schema] = registry.to_schemas(["subagent_spawn"])
    assert schema.input_schema["properties"]["skills"]["maxItems"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("skills", [["business-review"], "business-review"])
async def test_spawn_with_a_selected_skill_attaches_the_method_to_the_dispatched_run(monkeypatch, skills):
    runner = _Runner(admit=True)
    invocation = await _spawn(monkeypatch, runner, {"skills": skills})
    assert invocation.error is None, invocation.error
    required = runner.kwargs["required_model_input"]
    assert isinstance(required, RequiredSkillModelInput) and TEXT in required.content
    assert runner.kwargs["exact_binding"].execution_id == "aex_test"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"skills": []}])
async def test_spawn_without_skills_attaches_nothing(monkeypatch, args):
    runner = _Runner(admit=True)
    invocation = await _spawn(monkeypatch, runner, args)
    assert invocation.error is None, invocation.error
    assert runner.kwargs["profile_name"] == "compute"
    assert "required_model_input" not in runner.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["none", "current", "history"])
async def test_lead_request_advertises_skills_only_when_available(selection):
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from bowhead_router.router import AgentBinding, Router
    from test_skill_read_model_input import Adapter, Registry

    adapter = Adapter(["已完成。"])
    router = Router(Registry(primary=adapter))
    for agent_type in ("lead", "echo"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("primary", "test-model")))
    ctx = ToolExecutionContext(workspace_id="ws_1", member_uid="m", user_id="u_1", turn_id="t_1", datarc=None)
    skill = Message(role="user", content="SKILL METHOD", name=SKILL_CONTENT_MESSAGE_NAME)
    if selection == "current":
        ctx._selected_skills = (SELECTED,)
        ctx._selected_skill_messages = [skill]
    history = [Message(role="user", content="earlier"), Message(role="assistant", content="earlier answer")]
    if selection == "history":
        history.insert(1, selected_skill_messages([SELECTED], history=[])[0])
        ctx._history_skill_texts = history_skill_texts(history)
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = SubagentRunner(router=router, tool_registry=registry)
    await runner.run(
        profile_name="lead",
        task="分析2026年1-10月的经营情况",
        parent_turn_id="t_1",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
        history=history,
    )
    request = adapter.calls[0]
    spawn = next(tool for tool in request.tools if tool.name == "subagent_spawn")
    assert ("skills" in spawn.input_schema["properties"]) == (selection != "none")
    assert "skills" in registry.get("subagent_spawn").schema.input_schema["properties"]
    sent = request.messages
    index = next(i for i, message in enumerate(sent) if message.content == "分析2026年1-10月的经营情况")
    if selection == "current":
        assert sent[index + 1] == skill
    assert sent[: len(history)] == history


def _copy(name: str, body: str = "方法") -> Message:
    return Message(role="user", content=f'guidance\n\n<skill_content name="{name}">\n{body}\n</skill_content>', name=SKILL_CONTENT_MESSAGE_NAME)


def _history(*middle: Message) -> list[Message]:
    filler = [Message(role="user", content="q " + "x" * 400), Message(role="assistant", content="a " + "y" * 400)]
    return [*filler, *middle, *filler, Message(role="user", content="request")]


def test_history_compaction_skips_the_newest_copy_of_each_skill():
    messages = _history(_copy("review"), Message(role="assistant", content="ok"))
    current = len(messages) - 1
    start, end = eligible_context_range(messages, current_request=current, canonical_count=len(messages))
    assert not start <= 2 < end  # the kept copy is outside the candidate stretch
    assert end - start >= 2


def test_only_the_newest_copy_is_kept_and_notes_take_no_budget():
    note = Message(role="user", content='The Skill "review" selected for the request above is unchanged.', name=SKILL_CONTENT_MESSAGE_NAME)
    messages = _history(
        _copy("review", "旧"), Message(role="assistant", content="ok"), _copy("review", "新"), Message(role="assistant", content="ok"), note
    )
    from bowhead_core.context_view import _kept_skill_positions

    assert _kept_skill_positions(messages, len(messages) - 1) == [4]


def test_over_budget_copies_are_not_kept(monkeypatch):
    from bowhead_core import context_view

    monkeypatch.setattr(context_view, "SKILL_KEEP_TOKENS_EACH", 5)
    messages = _history(_copy("review", "很长的方法" * 50))
    assert context_view._kept_skill_positions(messages, len(messages) - 1) == []
    current = len(messages) - 1
    assert eligible_context_range(messages, current_request=current, canonical_count=len(messages)) == (0, current)


def test_history_without_skills_keeps_the_original_range():
    messages = _history()
    current = len(messages) - 1
    assert eligible_context_range(messages, current_request=current, canonical_count=len(messages)) == (0, current)
