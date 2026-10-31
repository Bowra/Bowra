"""Source selection through real Space, connection, identity and preference services."""

import logging
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_core.datarc_selection import DatarcPreferenceScope, current_datarc_selection, switch_datarc_selection
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_memory import UserMemoryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.connector_context_service import ConnectorContextService
from bowhead_gateway.datarc_context import resolve_datarc_recovery_context, resolve_team_datarc_binding
from bowhead_gateway.datarc_switch import DatarcContextSwitcher
from bowhead_gateway.member_resolver import MemberNotFoundError, MemberUidResolver
from cryptography.fernet import Fernet


class SourceClient:
    def __init__(self):
        self.projects = [{"id": "42", "name": "Retail"}, {"id": "7", "name": "Regional"}]
        self.revoked = set()
        self.calls = []

    async def fetch_account(self, email):
        self.calls.append(("account", email))
        return {"uid": "account_one"}

    async def list_account_projects(self, account_uid, **kwargs):
        self.calls.append(("projects", account_uid))
        return self.projects

    async def fetch_workspace_member(self, *, datarc_workspace_id, username):
        self.calls.append(("member", datarc_workspace_id))
        return None if datarc_workspace_id in self.revoked else {"uid": "member_" + datarc_workspace_id}


@pytest.fixture
async def datarc_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{path}", target="core") == ("core",)
    users = UserStore(path)
    team = await users.create_team(name="Selection tests", max_seats=5)
    user = await users.create_user(team_id=team.id, email="actor@example.com", display_name="Actor", email_verified=True)
    workspace = await users.create_local_workspace(team_id=team.id, name="Analysis", creator_user_id=user.id)
    connections = DatarcConnectionStore(path)
    connection = await connections.create_connection(team_id=team.id, name="Source", base_url="https://selection.datarc.example", auth_key="test-key")
    client = SourceClient()
    memory = UserMemoryStore(path)
    state = SimpleNamespace(
        user_store=users,
        user_memory_store=memory,
        datarc_connection_store=connections,
        datarc_client_factory=SimpleNamespace(get_client=lambda *_: client),
        member_resolver=MemberUidResolver(),
        tool_context_service=ConnectorContextService(user_store=users, user_memory_store=memory),
    )
    app = SimpleNamespace(state=state)
    binding = await resolve_team_datarc_binding(app, team.id)
    switcher = DatarcContextSwitcher(
        app, user_id=user.id, workspace_id=workspace, session_id="s_one", binding=binding, log=logging.getLogger(__name__)
    )
    ctx = ToolExecutionContext(
        workspace_id=workspace,
        team_id=team.id,
        user_id=user.id,
        session_id="s_one",
        turn_id="t_one",
        datarc=client,
        datarc_binding=binding,
        active_datarc_project="42",
        member_uid="member_42",
        datarc_context_switcher=switcher,
    )
    return SimpleNamespace(
        path=path,
        app=app,
        users=users,
        user=user,
        team=team,
        workspace=workspace,
        connection=connection,
        binding=binding,
        client=client,
        memory=memory,
        switcher=switcher,
        ctx=ctx,
        scope=DatarcPreferenceScope(team.id, connection.id),
    )


async def test_switch_uses_executing_space_team_despite_legacy_home_team(datarc_environment):
    env = datarc_environment
    other = await env.users.create_team(name="Legacy home", max_seats=5)
    with sqlite3.connect(env.path) as db:
        db.execute("UPDATE users SET team_id=? WHERE id=?", (other.id, env.user.id))
    result = await switch_datarc_selection(env.ctx, "7", "Regional", remember=False)
    assert result["persistence"]["status"] == "saved"
    assert current_datarc_selection(env.ctx).member_uid == "member_7"
    assert (await env.memory.get_datarc_workspace(env.user.id, env.scope, session_id="s_one"))["id"] == "7"
    assert ("member", "7") in env.client.calls


async def test_guide_questions_refuses_an_unreadable_space_before_source_access(datarc_environment):
    from bowhead_gateway.routes.datarc import guide_questions
    from fastapi import HTTPException

    env = datarc_environment
    team = await env.users.create_team(name="Other Team", max_seats=2)
    owner = await env.users.create_user(team_id=team.id, email="other@example.com", email_verified=True)
    space = await env.users.create_local_workspace(team_id=team.id, name="Private", creator_user_id=owner.id)
    with pytest.raises(HTTPException) as denied:
        await guide_questions(SimpleNamespace(app=env.app), workspace_id=space, user=env.user, store=env.users)
    assert denied.value.status_code in {403, 404}
    assert env.client.calls == []


async def test_switch_refresh_rejects_revoked_member_after_warm_identity_cache(datarc_environment):
    env = datarc_environment
    await env.app.state.member_resolver.resolve(env.user.email, "7", datarc=env.client)
    env.client.revoked.add("7")
    with pytest.raises(MemberNotFoundError):
        await switch_datarc_selection(env.ctx, "7", "Regional", remember=True)
    assert current_datarc_selection(env.ctx).datarc_workspace_id == "42"
    assert await env.memory.get_datarc_workspace(env.user.id, env.scope) is None
    assert env.client.calls.count(("member", "7")) == 2
    with pytest.raises(MemberNotFoundError):
        await env.app.state.member_resolver.resolve(env.user.email, "7", datarc=env.client)


async def test_switch_refresh_rejects_removed_workspace_after_warm_roster(datarc_environment):
    env = datarc_environment
    await env.app.state.tool_context_service.accessible_workspaces(env.client, env.user.id)
    env.client.projects = [{"id": "42", "name": "Retail"}]
    with pytest.raises(ValueError, match="datarc_workspace_unavailable"):
        await switch_datarc_selection(env.ctx, "7", "Regional", remember=False)
    assert current_datarc_selection(env.ctx).datarc_workspace_id == "42"
    assert await env.memory.get_datarc_workspace(env.user.id, env.scope, session_id="s_one") is None


async def test_rotation_within_same_timestamp_refuses_old_binding(datarc_environment, monkeypatch):
    from bowhead_core import datarc_connection_store as registry

    env = datarc_environment
    original = current_datarc_selection(env.ctx).snapshot().model_dump(mode="json")
    monkeypatch.setattr(registry, "_now", lambda: env.connection.updated_at)
    await env.app.state.datarc_connection_store.update_connection_auth(env.connection.id, env.team.id, auth_key="rotated-test-key")
    newer = await resolve_team_datarc_binding(env.app, env.team.id)
    assert newer.revision != env.binding.revision
    with pytest.raises(ValueError, match="datarc_connection_changed"):
        await switch_datarc_selection(env.ctx, "7", "Regional", remember=True)
    assert current_datarc_selection(env.ctx).datarc_workspace_id == "42"
    assert await env.memory.get_datarc_workspace(env.user.id, env.scope) is None
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        await resolve_datarc_recovery_context(
            env.app, team_id=env.team.id, user_id=env.user.id, workspace_id=env.workspace, session_id="s_one", evidence=original
        )


async def test_switch_publishes_the_same_connections_auxiliary_client(datarc_environment):
    from dataclasses import replace

    env = datarc_environment
    requested, vector_a, vector_b = [], object(), object()

    async def resolve_vector(projects):
        requested.append(projects)
        return vector_b

    env.ctx.datarc_binding = replace(env.ctx.datarc_binding, vector_resolver=resolve_vector)
    env.ctx.vector_client = vector_a
    env.switcher.binding = env.ctx.datarc_binding
    assert (await switch_datarc_selection(env.ctx, "7", "Regional", remember=False))["ok"]
    assert requested == [["7"]]
    assert env.ctx.vector_client is vector_b


async def test_recreated_recovery_reauthorizes_captured_source_instead_of_new_preference(datarc_environment):
    env = datarc_environment
    original = current_datarc_selection(env.ctx).snapshot().model_dump(mode="json")
    await switch_datarc_selection(env.ctx, "7", "Regional", remember=True)
    # Recreate the services whose caches belonged to the original worker.
    env.app.state.user_memory_store = UserMemoryStore(env.path)
    env.app.state.member_resolver = MemberUidResolver()
    env.app.state.tool_context_service = ConnectorContextService(user_store=env.users, user_memory_store=env.app.state.user_memory_store)
    recovered, member = await resolve_datarc_recovery_context(
        env.app,
        team_id=env.team.id,
        user_id=env.user.id,
        workspace_id=env.workspace,
        session_id="s_one",
        evidence=original,
    )
    assert (recovered.active_datarc_project, member) == ("42", "member_42")
    assert recovered.active_datarc_workspace_name == "Retail"
    assert recovered.accessible_datarc_workspaces == tuple(env.client.projects)
    assert (await env.memory.get_datarc_workspace(env.user.id, env.scope))["id"] == "7"
    env.client.revoked.add("42")
    with pytest.raises(MemberNotFoundError):
        await resolve_datarc_recovery_context(
            env.app, team_id=env.team.id, user_id=env.user.id, workspace_id=env.workspace, session_id="s_one", evidence=original
        )


async def test_recovery_refreshes_the_roster_instead_of_restoring_revoked_choices(datarc_environment):
    env = datarc_environment
    original = current_datarc_selection(env.ctx).snapshot().model_dump(mode="json")
    await env.app.state.tool_context_service.accessible_workspaces(env.client, env.user.id)
    env.client.projects = [{"id": "42", "name": "Retail"}, {"id": "9", "name": "New source"}]
    recovered, _ = await resolve_datarc_recovery_context(
        env.app, team_id=env.team.id, user_id=env.user.id, workspace_id=env.workspace, session_id="s_one", evidence=original
    )
    assert recovered.accessible_datarc_workspaces == tuple(env.client.projects)
    env.client.projects = [{"id": "9", "name": "New source"}]
    with pytest.raises(ValueError, match="datarc_resume_scope_changed"):
        await resolve_datarc_recovery_context(
            env.app, team_id=env.team.id, user_id=env.user.id, workspace_id=env.workspace, session_id="s_one", evidence=original
        )


async def test_recovery_rejects_an_unverified_identity_before_returning_the_roster(datarc_environment):
    env = datarc_environment
    original = current_datarc_selection(env.ctx).snapshot().model_dump(mode="json")
    with sqlite3.connect(env.path) as db:
        db.execute("UPDATE users SET email_verified=0 WHERE id=?", (env.user.id,))
    with pytest.raises(ValueError, match="datarc_identity_required"):
        await resolve_datarc_recovery_context(
            env.app, team_id=env.team.id, user_id=env.user.id, workspace_id=env.workspace, session_id="s_one", evidence=original
        )
    assert env.client.calls == []
