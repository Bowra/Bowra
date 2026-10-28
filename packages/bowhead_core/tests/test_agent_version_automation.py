from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.agent_version_automation import AgentVersionAutomationError, AgentVersionAutomationStore
from bowhead_core.openapi_store import OpenApiStore
from test_agent_revision_store import _db, _seed


async def _grant(db_path):
    key_store = OpenApiStore(str(db_path))
    key, _token = await key_store.create_key(
        team_id="team_release",
        name="CI",
        scopes=["agent_versions:read", "agent_versions:write"],
        expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    store = AgentVersionAutomationStore(str(db_path))
    grant = await store.create_grant(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        principal_id=key["id"],
        scopes=["proposal:create", "gate:read"],
        repositories=["acme/agent-*"],
        refs=["refs/heads/main", "refs/tags/v*"],
        expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
    )
    return store, grant, key


@pytest.mark.asyncio
async def test_automation_grant_requires_exact_scope_repository_and_current_owner(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, grant, key = await _grant(db_path)
    actor = await store.authorize(
        team_id="team_release",
        agent_id="osa_release",
        grant_id=grant["id"],
        principal_id=key["id"],
        required_scope="proposal:create",
        repository="acme/agent-finance",
        ref="refs/heads/main",
    )
    assert actor["effective_owner_user_id"] == "user_owner"
    principals = await store.list_transport_principals(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
    )
    assert principals == [
        {
            "id": key["id"],
            "name": "CI",
            "token_prefix": key["token_prefix"],
            "scopes": ["agent_versions:read", "agent_versions:write"],
            "expires_at": key["expires_at"],
        }
    ]
    for scope, repository, details in (
        ("eval:run", "acme/agent-finance", {"reason": "action_not_granted", "required_action": "eval:run"}),
        ("proposal:create", "other/repo", {"reason": "repository_not_allowed"}),
    ):
        with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied") as denied:
            await store.authorize(
                team_id="team_release",
                agent_id="osa_release",
                grant_id=grant["id"],
                principal_id=key["id"],
                required_scope=scope,
                repository=repository,
                ref="refs/heads/main",
            )
        assert denied.value.details == details
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE team_openapi_keys SET scopes_json='[\"users:read\"]' WHERE id=?",
            (key["id"],),
        )
        db.commit()
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied") as key_scope:
        await store.authorize(
            team_id="team_release",
            agent_id="osa_release",
            grant_id=grant["id"],
            principal_id=key["id"],
            required_scope="proposal:create",
            repository="acme/agent-finance",
            ref="refs/heads/main",
        )
    assert key_scope.value.details == {"reason": "key_scope_missing", "required_key_scope": "agent_versions:write"}
    with sqlite3.connect(db_path) as db:
        db.execute(
            'UPDATE team_openapi_keys SET scopes_json=\'["agent_versions:read","agent_versions:write"]\' WHERE id=?',
            (key["id"],),
        )
        db.execute("UPDATE team_subagent SET owner_user_id='user_member' WHERE id='osa_release'")
        db.commit()
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied") as owner:
        await store.authorize(
            team_id="team_release",
            agent_id="osa_release",
            grant_id=grant["id"],
            principal_id=key["id"],
            required_scope="proposal:create",
            repository="acme/agent-finance",
            ref="refs/heads/main",
        )
    assert owner.value.details == {"reason": "owner_changed"}


@pytest.mark.asyncio
async def test_dependency_policy_is_owner_scoped_and_uses_cas(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentVersionAutomationStore(str(db_path))

    created = await store.put_dependency_policy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        dependency_kind="skill",
        dependency_ref=" financial-analysis@2 ",
        policy="pinned",
        expected_row_version=0,
    )
    assert created["dependency_ref"] == "financial-analysis@2"
    updated = await store.put_dependency_policy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        dependency_kind="skill",
        dependency_ref="financial-analysis@2",
        policy="compatible",
        expected_row_version=created["row_version"],
    )
    assert updated["policy"] == "compatible"
    with pytest.raises(AgentVersionAutomationError, match="agent_dependency_policy_conflict"):
        await store.put_dependency_policy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            dependency_kind="skill",
            dependency_ref="financial-analysis@2",
            policy="notify_only",
            expected_row_version=created["row_version"],
        )
    with pytest.raises(AgentVersionAutomationError, match="agent_owner_required"):
        await store.list_dependency_policies(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_member",
        )


def _replacement(store, key, **overrides):
    return store.create_grant(
        **{
            "team_id": "team_release",
            "agent_id": "osa_release",
            "actor_user_id": "user_owner",
            "principal_id": key["id"],
            "scopes": ["proposal:create", "gate:read"],
            "repositories": ["acme/agent-finance"],
            "refs": ["refs/heads/main"],
            "expires_at": (datetime.now(UTC) + timedelta(days=14)).isoformat(),
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_replacing_a_grant_revokes_it_like_a_manual_revoke_in_one_transaction(tmp_path, monkeypatch) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    store, old, key = await _grant(db_path)
    new = await _replacement(store, key, replaces_grant_id=old["id"])

    grants = {item["id"]: item for item in await store.list_grants(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")}
    assert grants[new["id"]]["status"] == "active"
    assert grants[new["id"]]["repository_rules"] == {"repositories": ["acme/agent-finance"], "refs": ["refs/heads/main"]}
    assert grants[old["id"]]["status"] == "revoked"
    # The replaced grant stops authorizing at once, as after a manual revoke.
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied") as denied:
        await store.authorize(
            team_id="team_release",
            agent_id="osa_release",
            grant_id=old["id"],
            principal_id=key["id"],
            required_scope="proposal:create",
            repository="acme/agent-finance",
            ref="refs/heads/main",
        )
    assert denied.value.details == {"reason": "grant_revoked"}
    with sqlite3.connect(db_path) as db:
        audit = db.execute(
            "SELECT actor_user_id,target_id,payload_json FROM admin_audit_log WHERE action='agent_automation_grant_replaced'"
        ).fetchall()
    assert len(audit) == 1
    assert audit[0][:2] == ("user_owner", new["id"])
    assert json.loads(audit[0][2]) == {"agent_id": "osa_release", "grant_id": new["id"], "replaced_grant_id": old["id"]}


def _grant_count(db_path) -> int:
    with sqlite3.connect(db_path) as db:
        return int(db.execute("SELECT count(*) FROM agent_version_automation_grant").fetchone()[0])


@pytest.mark.asyncio
async def test_replacement_refuses_grants_the_owner_cannot_replace_without_creating_one(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, old, key = await _grant(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        columns = [row[1] for row in db.execute("PRAGMA table_info(team_subagent)")]
        values = ", ".join({"id": "'osa_other'", "name": "'other-agent'", "display_name": "'Other Agent'"}.get(column, column) for column in columns)
        db.execute(f"INSERT INTO team_subagent({','.join(columns)}) SELECT {values} FROM team_subagent WHERE id='osa_release'")
        db.commit()
    other_agent = await _replacement(store, key, agent_id="osa_other")
    revoked = await _replacement(store, key)
    await store.revoke_grant(
        team_id="team_release", agent_id="osa_release", grant_id=revoked["id"], actor_user_id="user_owner", expected_row_version=1
    )
    before = _grant_count(db_path)

    for replaced in (other_agent["id"], revoked["id"], "avag_unknown"):
        with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_not_found"):
            await _replacement(store, key, replaces_grant_id=replaced)
    assert _grant_count(db_path) == before

    # A member who is not the Owner cannot replace (or revoke) the Owner's grant.
    with pytest.raises(AgentVersionAutomationError, match="agent_owner_required"):
        await _replacement(store, key, actor_user_id="user_member", replaces_grant_id=old["id"])
    assert _grant_count(db_path) == before


@pytest.mark.asyncio
async def test_new_owner_replaces_a_previous_owners_grant_like_a_revoke(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, old, key = await _grant(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_member' WHERE id='osa_release'")
        db.commit()
    # The previous Owner's grant no longer authorizes; the new Owner sees it and may revoke it,
    # so replacing it is allowed too.
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied") as denied:
        await store.authorize(
            team_id="team_release",
            agent_id="osa_release",
            grant_id=old["id"],
            principal_id=key["id"],
            required_scope="proposal:create",
            repository="acme/agent-finance",
            ref="refs/heads/main",
        )
    assert denied.value.details == {"reason": "owner_changed"}

    new = await _replacement(store, key, actor_user_id="user_member", replaces_grant_id=old["id"])

    grants = {item["id"]: item for item in await store.list_grants(team_id="team_release", agent_id="osa_release", actor_user_id="user_member")}
    assert grants[old["id"]]["status"] == "revoked"
    assert grants[new["id"]]["status"] == "active"
    authorized = await store.authorize(
        team_id="team_release",
        agent_id="osa_release",
        grant_id=new["id"],
        principal_id=key["id"],
        required_scope="proposal:create",
        repository="acme/agent-finance",
        ref="refs/heads/main",
    )
    assert authorized["effective_owner_user_id"] == "user_member"


@pytest.mark.asyncio
async def test_failed_replacement_insert_leaves_the_old_grant_active(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, old, key = await _grant(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TRIGGER fail_grant_insert BEFORE INSERT ON agent_version_automation_grant
            BEGIN SELECT RAISE(ABORT, 'insert failed'); END"""
        )
        db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await _replacement(store, key, replaces_grant_id=old["id"])
    grants = await store.list_grants(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")
    assert [(item["id"], item["status"]) for item in grants] == [(old["id"], "active")]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM admin_audit_log WHERE action='agent_automation_grant_replaced'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_the_retired_webhook_scope_is_refused_for_new_grants_and_ignored_on_stored_ones(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store, grant, key = await _grant(db_path)
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_scope_invalid"):
        await _replacement(store, key, scopes=["proposal:create", "webhook:receive"])
    # A grant stored before webhooks were removed may still list the scope; it is not reported.
    with sqlite3.connect(db_path) as db:
        # Simulate a grant written before the removal; stored grants are otherwise immutable.
        trigger = db.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND sql LIKE '%automation_grant binding is immutable%'"
        ).fetchone()
        db.execute(f"DROP TRIGGER {trigger[0]}")
        db.execute(
            "UPDATE agent_version_automation_grant SET scopes_json=?,row_version=row_version+1 WHERE id=?",
            ('["gate:read","proposal:create","webhook:receive"]', grant["id"]),
        )
        db.execute(trigger[1])
    grants = await store.list_grants(team_id="team_release", agent_id="osa_release", actor_user_id="user_owner")
    assert [item["scopes"] for item in grants if item["id"] == grant["id"]] == [["gate:read", "proposal:create"]]
