"""Per-call revalidation of an automation grant.

`authorize()` is the only thing standing between a CI token and an Agent's release machinery,
and it re-evaluates every condition on every call rather than trusting the grant it was issued.
These cover the legs that had no test: the owner's account/membership/team still being live,
the agent not being archived, the grant not being revoked or expired, and the ref matching.
Each case carries a positive control so a guard that denies everything cannot pass as correct.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from bowhead_core.agent_version_automation import (
    AgentVersionAutomationError,
    AgentVersionAutomationStore,
)
from bowhead_core.openapi_store import OpenApiStore
from test_agent_revision_store import _db, _seed


async def _setup(tmp_path, *, expires_in: timedelta = timedelta(days=7)):
    db_path = _db(tmp_path)
    _seed(db_path)
    key, _token = await OpenApiStore(str(db_path)).create_key(
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
        refs=["refs/heads/main"],
        expires_at=(datetime.now(UTC) + expires_in).isoformat(),
    )
    return db_path, store, grant, key


async def _authorize(store, grant, key, *, ref: str = "refs/heads/main") -> dict:
    return await store.authorize(
        team_id="team_release",
        agent_id="osa_release",
        grant_id=grant["id"],
        principal_id=key["id"],
        required_scope="proposal:create",
        repository="acme/agent-finance",
        ref=ref,
    )


def _mutate(db_path, statement: str) -> None:
    with sqlite3.connect(str(db_path)) as db:
        db.execute(statement)
        db.commit()


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("membership removed", "UPDATE team_members SET status='removed' WHERE team_id='team_release' AND user_id='user_owner'"),
        ("account disabled", "UPDATE users SET status='disabled' WHERE id='user_owner'"),
        ("team suspended", "UPDATE teams SET status='suspended' WHERE id='team_release'"),
    ],
)
@pytest.mark.asyncio
async def test_grant_does_not_outlive_its_owner(tmp_path, label: str, statement: str) -> None:
    """An offboarded Owner's pipeline must stop working.

    The human path (`_require_owner_conn`) already gates on team / account / membership; the
    automation path applies the same three. Each leg is its own case so a partial regression
    cannot hide behind the other two.
    """
    db_path, store, grant, key = await _setup(tmp_path / label.replace(" ", "_"))
    assert (await _authorize(store, grant, key))["kind"] == "automation"

    _mutate(db_path, statement)

    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied"):
        await _authorize(store, grant, key)


@pytest.mark.asyncio
async def test_archived_agent_and_foreign_ref_are_denied(tmp_path) -> None:
    db_path, store, grant, key = await _setup(tmp_path / "legs")
    assert (await _authorize(store, grant, key))["kind"] == "automation"

    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied"):
        await _authorize(store, grant, key, ref="refs/heads/attacker")

    _mutate(db_path, "UPDATE team_subagent SET lifecycle_state='archived' WHERE id='osa_release'")
    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied"):
        await _authorize(store, grant, key)


@pytest.mark.asyncio
async def test_expired_grant_is_denied(tmp_path) -> None:
    """`expires_at` is immutable at the DB, so expiry can only be observed by letting it pass."""
    _db_path, store, grant, key = await _setup(tmp_path / "expiry", expires_in=timedelta(seconds=2))
    assert (await _authorize(store, grant, key))["kind"] == "automation"

    await anyio.sleep(2.2)

    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied"):
        await _authorize(store, grant, key)


@pytest.mark.asyncio
async def test_revocation_cannot_be_starved_by_the_credential_it_revokes(tmp_path) -> None:
    """Revoking must not lose a race against the pipeline being revoked.

    `authorize` moves row_version to stamp last_used_at. When revoke also demanded that exact
    row_version, a CI loop between the Owner loading the page and pressing revoke kept the grant
    alive — failing precisely in the compromised-token case revocation exists for.
    """
    _db_path, store, grant, key = await _setup(tmp_path / "starve")
    seen_by_owner = int(grant["row_version"])

    for _ in range(3):  # the pipeline keeps calling while the Owner decides
        await _authorize(store, grant, key)

    revoked = await store.revoke_grant(
        team_id="team_release",
        agent_id="osa_release",
        grant_id=grant["id"],
        actor_user_id="user_owner",
        expected_row_version=seen_by_owner,
    )
    assert revoked["status"] == "revoked"

    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_denied"):
        await _authorize(store, grant, key)


@pytest.mark.asyncio
async def test_revoking_twice_is_success_and_unknown_grant_is_not_found(tmp_path) -> None:
    _db_path, store, grant, _key = await _setup(tmp_path / "idempotent")
    for expected in (int(grant["row_version"]), 999):
        again = await store.revoke_grant(
            team_id="team_release",
            agent_id="osa_release",
            grant_id=grant["id"],
            actor_user_id="user_owner",
            expected_row_version=expected,
        )
        assert again["status"] == "revoked"

    with pytest.raises(AgentVersionAutomationError, match="agent_automation_grant_not_found"):
        await store.revoke_grant(
            team_id="team_release",
            agent_id="osa_release",
            grant_id="avag_does_not_exist",
            actor_user_id="user_owner",
            expected_row_version=1,
        )


@pytest.mark.parametrize(
    "repository",
    [
        "acme/agent-x/../../../evil-org/backdoor",
        "acme/agent-..",
        "/acme/agent-x",
        "acme/agent-x/",
        "acme//agent-x",
        "acme/agent-x\n",
        "acme/agent-\x00x",
    ],
)
@pytest.mark.asyncio
async def test_malformed_self_reported_context_is_rejected(tmp_path, repository: str) -> None:
    """The caller's self-reported repository must be readable before it is recorded.

    Nothing proves the request really came from that repository — without signed provenance from
    the forge it is a label, not a boundary. But the Owner's rules already reject `..`, and the
    audit log keeps whatever the caller sent, so the incoming value gets held to the same shape.
    """
    _db_path, store, grant, key = await _setup(tmp_path / "ctx")
    assert (await _authorize(store, grant, key))["kind"] == "automation"  # positive control

    with pytest.raises(AgentVersionAutomationError, match="agent_automation_context_invalid"):
        await store.authorize(
            team_id="team_release",
            agent_id="osa_release",
            grant_id=grant["id"],
            principal_id=key["id"],
            required_scope="proposal:create",
            repository=repository,
            ref="refs/heads/main",
        )


@pytest.mark.asyncio
async def test_a_pattern_may_still_span_path_separators(tmp_path) -> None:
    """`refs/heads/*` has to keep matching `refs/heads/feature/foo`.

    Narrowing `*` to stop at `/` looks like a tightening, but git branch names legitimately contain
    slashes and GitLab nests groups — it would silently stop matching refs Owners deliberately
    allowed. The separator-spanning behaviour is intentional; this pins it so a later "cleanup"
    has to argue with a test instead of quietly breaking real pipelines.
    """
    db_path = _db(tmp_path / "spanning")
    _seed(db_path)
    key, _token = await OpenApiStore(str(db_path)).create_key(
        team_id="team_release",
        name="CI",
        scopes=["agent_versions:write"],
        expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    store = AgentVersionAutomationStore(str(db_path))
    grant = await store.create_grant(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        principal_id=key["id"],
        scopes=["proposal:create"],
        repositories=["acme/*"],
        refs=["refs/heads/*"],
        expires_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
    )

    actor = await store.authorize(
        team_id="team_release",
        agent_id="osa_release",
        grant_id=grant["id"],
        principal_id=key["id"],
        required_scope="proposal:create",
        repository="acme/group/nested-project",
        ref="refs/heads/feature/some-work",
    )
    assert actor["kind"] == "automation"
