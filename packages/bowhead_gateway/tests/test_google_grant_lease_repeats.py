"""Google Workspace grant writes during a tool call survive a lost authority lease.

The grant row is fenced, so a write fails at once while a running Turn holds the core lease
(census T3/T4, docs/sprints/fenced-write-census-2026-11-17). A usage timestamp is skipped; a
re-authorization mark repeats, and a spent wait keeps the re-authorization error.
"""

from __future__ import annotations

import pytest
from bowhead_core.builtin_tool_grants import BuiltinToolGrantStore
from bowhead_core.google_workspace import GoogleWorkspaceError, token_broker
from test_google_workspace_routes import _DOMAIN, _authorize, _configure, _tokens, env, google  # noqa: F401 - fixtures
from test_request_write_lease_repeats import _lose_lease

from bowhead_core import _db

pytestmark = pytest.mark.anyio


async def _broker(env):  # noqa: F811
    connection = await _configure(env)
    await _authorize(env, env.admin, token_body=_tokens(f"admin@{_DOMAIN}"))
    tool = await env.store.get_team_builtin_tool(env.team.id, "google_workspace")
    auth = await env.store.get_team_builtin_tool_instance_auth_decrypted(env.team.id, tool.id)
    return connection, tool, auth, token_broker(env.db_path)


async def test_a_lost_lease_on_the_usage_timestamp_does_not_fail_the_refresh(env, monkeypatch) -> None:  # noqa: F811
    connection, tool, auth, broker = await _broker(env)
    before = (await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id)).last_used_at
    attempts = _lose_lease(monkeypatch, "builtin_tool_user_grant", 100)

    env.google.token_responses.append((200, {"access_token": "fresh", "expires_in": 3600}))
    token, _ = await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))

    assert token == "fresh"
    assert attempts == [1]  # skipped, not repeated
    assert (await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id)).last_used_at == before


async def test_the_reauth_mark_repeats_after_a_lost_lease(env, monkeypatch) -> None:  # noqa: F811
    connection, tool, auth, broker = await _broker(env)
    attempts = _lose_lease(monkeypatch, "builtin_tool_user_grant", 1)

    env.google.token_responses.append((400, {"error": "invalid_grant"}))
    with pytest.raises(GoogleWorkspaceError) as reauth:
        await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))

    assert reauth.value.code == "google_reauth_required"
    assert attempts == [1, 2]
    assert (await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id)).status == "needs_reauth"


async def test_a_spent_lease_wait_keeps_the_reauth_error(env, monkeypatch) -> None:  # noqa: F811
    connection, tool, auth, broker = await _broker(env)
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.05)
    _lose_lease(monkeypatch, "builtin_tool_user_grant", 10_000)

    env.google.token_responses.append((400, {"error": "invalid_grant"}))
    with pytest.raises(GoogleWorkspaceError) as reauth:
        await broker.access_token(tool=tool, auth=auth, user_id=env.admin.id, required_bundles=frozenset({"base"}))

    assert reauth.value.code == "google_reauth_required"
    assert (await BuiltinToolGrantStore(env.db_path).get(connection["id"], env.admin.id)).status == "active"
