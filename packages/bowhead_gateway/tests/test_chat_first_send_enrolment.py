"""新会话首条消息的「尽力而为自动入组」（``BOWHEAD_WS_MEMBERSHIP_ENFORCE`` 关时，``chat.py``）。

- 从来不是该空间成员的人：首发后自动入组——这是今天的兼容行为，保持不变（正对照）。
- **已被管理员移出**的人：首发后仍是移出。此前 ``get_member_role`` 只认 active，被移出的人查不到角色 ⇒
  ``add_member``（``INSERT OR REPLACE``）把 removed 行改回 active：发一条消息就回来了，移出与空间隔离都形同虚设。
  判「从来不是成员」必须用 ``has_member_record``（任何状态的成员行都算），与 ws-autosync 同一个判据。

只断言成员行的状态，不断言本次请求的状态码：这条路后面还有模型 / 编排等依赖，夹具里不装它们。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
async def space(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", "0")
    store = UserStore(tmp_path / "enrol.db")
    await store.initialize()
    team = await store.create_team(name="T")
    owner = await store.create_user(team_id=team.id, email="owner@example.com", password="StrongPass123!")
    await store.set_team_creator(team.id, owner.id)
    member = await store.create_user(team_id=team.id, email="member@example.com", password="StrongPass123!")
    workspace_id = await store.create_local_workspace(team_id=team.id, name="S", creator_user_id=owner.id)

    app = FastAPI()
    app.state.settings = SimpleNamespace(dev_default_user="dev_user", auth_mode="dev")
    app.state.user_store = store
    app.state.orchestrator = SimpleNamespace()
    app.include_router(chat_routes.router, prefix="/api/v1/chat")

    async def resolve(_request):
        return member.id, team.id

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", resolve)

    def first_send() -> None:
        TestClient(app, raise_server_exceptions=False).post("/api/v1/chat/send", json={"workspace_id": workspace_id, "message": "hi"})

    return SimpleNamespace(store=store, workspace_id=workspace_id, member=member, first_send=first_send)


async def test_first_send_enrols_someone_who_was_never_a_member(space) -> None:
    assert await space.store.get_member_role(space.workspace_id, space.member.id) is None
    space.first_send()
    assert await space.store.get_member_role(space.workspace_id, space.member.id) == "member"


async def test_first_send_does_not_bring_back_a_removed_member(space) -> None:
    await space.store.add_member(workspace_id=space.workspace_id, user_id=space.member.id, role="member")
    assert await space.store.remove_member(space.workspace_id, space.member.id) is True
    space.first_send()
    assert await space.store.get_member_role(space.workspace_id, space.member.id) is None
    assert await space.store.has_member_record(space.workspace_id, space.member.id) is True  # Same removed row, neither deleted nor restored.
