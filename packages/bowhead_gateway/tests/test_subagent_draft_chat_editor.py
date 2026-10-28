"""Chat edits remain scoped to the saved owner draft and submitted version."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bowhead_gateway.routes.chat import ChatSendRequest
from pydantic import ValidationError

from bowhead_gateway import subagent_draft_chat_editor as module


def _run(coro):
    return asyncio.run(coro)


def _draft(version: int = 3):
    return SimpleNamespace(
        id="sad_owned",
        row_version=version,
        entry_mode="conversational",
        status="ready",
        definition={
            "name": "my-agent",
            "display_name": "我的智能体",
            "description": "旧用途",
            "system_prompt": "Use evidence.",
            "model": None,
            "max_iterations": 10,
            "output_schema": None,
            "tool_refs": ["web_fetch"],
            "visibility": "creator_only",
        },
        recommendations=[{"tool_ref": "web_fetch", "display_name": "读取网页", "selected": True, "locked_reason": None}],
        validation_status="valid",
    )


def test_inspect_and_edit_keep_owner_scope_and_cas(monkeypatch) -> None:
    draft = _draft()
    owner = SimpleNamespace(id="owner", team_id="team")
    calls = []

    async def owned(_request, user, draft_id):
        calls.append((user.team_id, user.id, draft_id))
        return draft

    async def writable(_request):
        return None

    async def review_update(*, body, expected_version, **_kwargs):
        assert expected_version == 3
        assert body.definition.description == "新用途"
        assert body.selected_tool_refs == ["web_fetch"]
        return _draft(version=4)

    monkeypatch.setattr(module, "_owned_draft", owned)
    monkeypatch.setattr(module, "resolve_subagent_authoring_capabilities", lambda _team: SimpleNamespace(draft_chat_edit=True, unified_draft=False))
    monkeypatch.setattr(module, "assert_not_readonly", writable)
    monkeypatch.setattr(module, "_review_update", review_update)
    monkeypatch.setattr(module, "SubagentDraft", SimpleNamespace)
    editor = module.SubagentDraftChatEditor(SimpleNamespace(), owner, "sad_owned", 3)
    assert _run(editor.inspect())["definition"]["description"] == "旧用途"
    result = _run(editor.edit({"description": "新用途"}))
    assert result["applied"] is True and result["version"] == 4
    assert result["message"] == "右侧草稿已更新。请核对配置，并在确认后创建智能体。"
    assert calls == [("team", "owner", "sad_owned"), ("team", "owner", "sad_owned")]


def test_stale_or_unsaved_draft_does_not_write(monkeypatch) -> None:
    async def stale(_request, _user, _draft_id):
        return _draft(version=4)

    async def writable(_request):
        return None

    monkeypatch.setattr(module, "_owned_draft", stale)
    monkeypatch.setattr(module, "resolve_subagent_authoring_capabilities", lambda _team: SimpleNamespace(draft_chat_edit=True))
    monkeypatch.setattr(module, "assert_not_readonly", writable)
    owner = SimpleNamespace(id="owner", team_id="team")
    editor = module.SubagentDraftChatEditor(SimpleNamespace(), owner, "sad_owned", 3)
    assert _run(editor.edit({"description": "cannot apply"}))["outcome"] == "version_conflict"
    dirty = module.SubagentDraftChatEditor(SimpleNamespace(), owner, "sad_owned", 3, True)
    inspected = _run(dirty.inspect())
    assert inspected == {"ok": False, "message": "右侧有未保存修改。请先保存或放弃右侧修改，再继续对话编辑。"}
    assert _run(dirty.edit({"description": "cannot apply"})) == {
        "ok": False,
        "applied": False,
        "message": "右侧有未保存修改。请先保存或放弃右侧修改，再继续对话编辑。",
    }


def test_converted_manual_draft_is_not_edited_by_old_chat_binding(monkeypatch) -> None:
    converted = _draft()
    converted.entry_mode = "canvas"

    async def owned(_request, _user, _draft_id):
        return converted

    async def writable(_request):
        return None

    monkeypatch.setattr(module, "_owned_draft", owned)
    monkeypatch.setattr(module, "resolve_subagent_authoring_capabilities", lambda _team: SimpleNamespace(draft_chat_edit=True, unified_draft=False))
    monkeypatch.setattr(module, "assert_not_readonly", writable)
    owner = SimpleNamespace(id="owner", team_id="team")
    editor = module.SubagentDraftChatEditor(SimpleNamespace(), owner, "sad_owned", 3)
    assert _run(editor.inspect()) == {"ok": False, "outcome": "draft_unavailable_or_changed"}
    assert _run(editor.edit({"description": "cannot apply"}))["applied"] is False


def test_manual_draft_chat_edit_uses_same_version_and_space(monkeypatch) -> None:
    draft = _draft()
    draft.entry_mode = "canvas"
    draft.workspace_id = "ws_a"
    draft.purpose_text = "阅读给定网页"
    draft.warnings = []
    saved = _draft(version=4)
    saved.entry_mode = "canvas"
    saved.definition = {**draft.definition, "description": "仅阅读给定网页", "output_schema_text": ""}
    calls = []

    class FakeUserStore:
        async def list_workspaces_for_user(self, user_id, *, team_id):
            assert (user_id, team_id) == ("owner", "team")
            return [{"workspace_id": "ws_a"}]

    class FakeDraftStore:
        async def save_manual_draft(self, draft_id, **kwargs):
            calls.append((draft_id, kwargs))
            return saved

    async def owned(_request, _user, _draft_id):
        return draft

    async def draft_store(_request):
        return FakeDraftStore()

    async def writable(_request):
        return None

    monkeypatch.setattr(module, "_owned_draft", owned)
    monkeypatch.setattr(module, "_draft_store", draft_store)
    monkeypatch.setattr(module, "UserStore", FakeUserStore)
    monkeypatch.setattr(module, "resolve_subagent_authoring_capabilities", lambda _team: SimpleNamespace(draft_chat_edit=True, unified_draft=True))
    monkeypatch.setattr(module, "assert_not_readonly", writable)
    monkeypatch.setattr(module, "SubagentDraft", SimpleNamespace)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=FakeUserStore())))
    owner = SimpleNamespace(id="owner", team_id="team")
    wrong_space = module.SubagentDraftChatEditor(request, owner, draft.id, 3, workspace_id="ws_b")
    assert _run(wrong_space.inspect())["outcome"] == "draft_unavailable_or_changed"
    editor = module.SubagentDraftChatEditor(request, owner, draft.id, 3, workspace_id="ws_a")
    result = _run(editor.edit({"description": "仅阅读给定网页"}))
    assert result["applied"] is True and result["version"] == 4
    assert calls[0][0] == draft.id
    assert calls[0][1]["workspace_id"] == "ws_a"
    assert calls[0][1]["expected_version"] == 3
    assert calls[0][1]["definition"]["description"] == "仅阅读给定网页"


def test_append_uses_saved_value_and_rejects_ambiguous_operations(monkeypatch) -> None:
    draft = _draft()
    owner = SimpleNamespace(id="owner", team_id="team")
    calls = []

    async def owned(_request, _user, _draft_id):
        return draft

    async def writable(_request):
        return None

    async def review_update(*, body, **_kwargs):
        calls.append(body.definition.description)
        updated = _draft(version=4)
        updated.definition = {**updated.definition, "description": body.definition.description}
        return updated

    monkeypatch.setattr(module, "_owned_draft", owned)
    monkeypatch.setattr(module, "resolve_subagent_authoring_capabilities", lambda _team: SimpleNamespace(draft_chat_edit=True))
    monkeypatch.setattr(module, "assert_not_readonly", writable)
    monkeypatch.setattr(module, "_review_update", review_update)
    monkeypatch.setattr(module, "SubagentDraft", SimpleNamespace)
    editor = module.SubagentDraftChatEditor(SimpleNamespace(), owner, "sad_owned", 3)
    operation = {"field": "description", "operation": "append", "text": "测试版"}
    result = _run(editor.edit({}, [operation]))
    assert calls == ["旧用途测试版"]
    assert result["draft_description"] == "旧用途测试版"
    assert result["changed_fields"] == ["description"]
    assert _run(editor.edit({"description": "另一个值"}, [operation]))["outcome"] == "invalid_fields"
    assert _run(editor.edit({}, [operation, operation]))["outcome"] == "invalid_fields"
    assert _run(editor.edit({}, [{**operation, "text": "x" * 2001}]))["outcome"] == "invalid_fields"
    assert calls == ["旧用途测试版"]


def test_chat_submission_rejects_incomplete_or_malformed_draft_binding() -> None:
    valid = ChatSendRequest(
        workspace_id="ws_1",
        message="修改草稿",
        client_info={"agent_draft_id": "sad_owned", "agent_draft_version": "3"},
    )
    assert valid.client_info["agent_draft_version"] == "3"
    # A malformed draft id is refused by the shared AgentDraftSendTarget identity pattern
    # (ee63134a8); a missing version keeps the binding-level message and version 0 fails the same model's range.
    for client_info, message in (
        ({"agent_draft_id": "sad_owned"}, "invalid agent draft binding"),
        ({"agent_draft_id": "sad_../../other", "agent_draft_version": "3"}, r"String should match pattern '\^sad_"),
        ({"agent_draft_id": "sad_owned", "agent_draft_version": "0"}, "greater than or equal to 1"),
    ):
        with pytest.raises(ValidationError, match=message):
            ChatSendRequest(workspace_id="ws_1", message="修改草稿", client_info=client_info)
