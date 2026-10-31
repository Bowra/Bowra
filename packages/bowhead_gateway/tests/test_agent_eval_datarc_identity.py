"""The Datarc identity of an evaluation attempt (design agent-eval-datarc-read-only.md §3, real SQLite).

An attempt reads as the Agent's current Owner, only when the Owner started the run, through the Team
connection registry alone.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_bi_bridge.errors import BIConnectionError, BIPermissionError
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_eval_datarc import EvalDatarcLiveRun, EvalDatarcUnavailable
from bowhead_core.datarc_connection_store import DatarcConnectionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.agent_eval_datarc import TeamOwnerDatarcResolver
from bowhead_gateway.member_resolver import MemberUidResolver
from cryptography.fernet import Fernet

from bowhead_gateway import datarc_context

pytestmark = pytest.mark.anyio

AGENT_ID = "osa_datarc_eval"


class FakeClient:
    def __init__(self, *, member: str | None = "member_owner", member_error: Exception | None = None) -> None:
        self.member = member
        self.member_error = member_error
        self.lookups: list[tuple[str, str]] = []

    async def fetch_workspace_member(self, *, datarc_workspace_id: str, username: str) -> dict[str, Any] | None:
        self.lookups.append((datarc_workspace_id, username))
        if self.member_error is not None:
            raise self.member_error
        return {"uid": self.member} if self.member is not None else None


class FakeFactory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.keys: list[tuple[str, str]] = []

    def get_client(self, base_url: str, auth_key: str) -> FakeClient:
        self.keys.append((base_url, auth_key))
        return self.client


class Untouchable:
    """A process-global client that must never be used: there is no fallback."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"process-global Datarc client used: {name}")


async def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, verified: bool = True, connection: bool = True) -> SimpleNamespace:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    users = UserStore(db_path)
    team = await users.create_team(name="Datarc eval", max_seats=5)
    owner = await users.create_user(team_id=team.id, email="owner@example.com", display_name="Olivia", email_verified=verified)
    admin = await users.create_user(team_id=team.id, email="admin@example.com", display_name="Adam", email_verified=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO team_subagent(id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,status,created_at,updated_at)
            VALUES (?,?,'data-agent','Data Agent','Reads data','[]','[]','private',?,'enabled','2026-10-22T00:00:00Z','2026-10-22T00:00:00Z')
            """,
            (AGENT_ID, team.id, owner.id),
        )
        db.commit()
    store = DatarcConnectionStore(db_path)
    await store.initialize()
    if connection:
        await store.create_connection(team_id=team.id, name="Datarc", base_url="https://demo.datarc.example", auth_key="team-key")
    client = FakeClient()
    workspace_calls: list[dict[str, Any]] = []

    async def _ladder(state: Any, datarc_client: Any, user_id: str | None, **kwargs: Any) -> tuple[str | None, str, tuple[dict, ...], str]:
        workspace_calls.append({"user_id": user_id, **kwargs})
        return "21", "Retail", (), ""

    monkeypatch.setattr(datarc_context, "_resolve_per_user_datarc_workspace", _ladder)
    state = SimpleNamespace(
        datarc_connection_store=store,
        datarc_client_factory=FakeFactory(client),
        member_resolver=MemberUidResolver(),
        embed_client_factory=None,
        # A process-global client and Datarc environment variables exist; neither may be used.
        datarc_client=Untouchable(),
    )
    monkeypatch.setenv("DATARC_BASE_URL", "https://global.datarc.example")
    monkeypatch.setenv("DATARC_AUTH_KEY", "global-key")
    return SimpleNamespace(
        db_path=str(db_path),
        users=users,
        team=team,
        owner=owner,
        admin=admin,
        state=state,
        client=client,
        workspace_calls=workspace_calls,
        store=store,
    )


def _run(env: SimpleNamespace, *, created_by: str) -> Any:
    return SimpleNamespace(
        run_id="aer_datarc",
        created_by=created_by,
        subject=SimpleNamespace(team_id=env.team.id, agent_id=AGENT_ID, agent_source_kind="team"),
    )


@pytest.mark.parametrize(
    ("setting", "source_kind", "enabled"),
    [(None, "team", True), ("1", "team", True), ("0", "team", False), (None, "system", False)],
    ids=["default-on", "explicit-on", "explicit-off", "system-agent-excluded"],
)
async def test_runtime_defaults_to_owner_reads_and_preserves_explicit_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str | None, source_kind: str, enabled: bool
) -> None:
    from bowhead_gateway.agent_eval_runtime import _datarc_live_scope

    env = await _setup(tmp_path, monkeypatch)
    if setting is None:
        monkeypatch.delenv("FEATURE_AGENT_EVAL_DATARC_READ_ONLY", raising=False)
    else:
        monkeypatch.setenv("FEATURE_AGENT_EVAL_DATARC_READ_ONLY", setting)
    run = _run(env, created_by=env.owner.id)
    run.subject.agent_source_kind = source_kind

    scope = _datarc_live_scope(env.db_path, env.state, run)

    if not enabled:
        assert scope is None
        assert env.client.lookups == []
        return
    assert isinstance(scope, EvalDatarcLiveRun)
    identity = await scope.resolver.resolve(run)
    assert identity.member_uid == "member_owner"
    assert env.client.lookups == [("21", "owner@example.com")]
    assert env.state.datarc_client_factory.keys == [("https://demo.datarc.example", "team-key")]


async def test_the_owner_who_started_the_run_reads_as_their_own_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Owner start, Coach start and an automation grant all record the Owner as the run's actor.
    env = await _setup(tmp_path, monkeypatch)
    identity = await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert identity.member_uid == "member_owner" and identity.workspace_id == "21" and identity.workspace_name == "Retail"
    # The member came from the Owner's verified email, through the Team connection's own client.
    assert env.client.lookups == [("21", "owner@example.com")]
    assert identity.client is env.client
    assert env.state.datarc_client_factory.keys == [("https://demo.datarc.example", "team-key")]
    # The ladder ran read-only for the Owner, with no session: no auto-select, no preference writes.
    [call] = env.workspace_calls
    binding = call.pop("connection_binding")
    assert binding.team_id == env.team.id and binding.client is env.client
    assert call == {"user_id": env.owner.id, "session_id": None, "read_only": True, "log": call["log"]}


async def test_an_administrator_start_is_refused_before_any_datarc_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch)
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.admin.id))
    assert caught.value.cause == "starter_not_owner" and caught.value.owner_name == "Olivia"
    assert env.client.lookups == [] and env.state.datarc_client_factory.keys == [] and env.workspace_calls == []


async def test_an_ownership_transfer_refuses_the_next_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch)
    resolver = TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state)
    run = _run(env, created_by=env.owner.id)
    await resolver.resolve(run)
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id=? WHERE id=?", (env.admin.id, AGENT_ID))
        db.commit()
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await resolver.resolve(run)
    assert caught.value.cause == "starter_not_owner"


async def test_a_removed_owner_is_no_longer_the_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch)
    with sqlite3.connect(env.db_path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (env.owner.id,))
        db.commit()
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == "starter_not_owner"


async def test_an_unverified_owner_email_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch, verified=False)
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == "owner_identity_unverified"
    assert env.client.lookups == []


async def test_without_a_team_connection_there_is_no_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch, connection=False)
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == "no_connection"
    # Neither the process-global client (Untouchable raises on any use) nor the environment was used.
    assert env.state.datarc_client_factory.keys == [] and env.client.lookups == []


async def test_an_unusable_connection_names_its_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch)
    # The key can no longer be decrypted (the instance root secret changed).
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == "connection_unusable"


@pytest.mark.parametrize(
    ("member", "error", "cause"),
    [
        (None, None, "owner_not_member"),
        # A blank member from Datarc must never reach a Tool: it would turn the permission filter off.
        ("   ", None, "owner_not_member"),
        ("member_owner", BIPermissionError("datarc 401: access rejected", source="http"), "connection_rejected"),
        ("member_owner", BIConnectionError("datarc 5xx: 502"), "datarc_unreachable"),
    ],
)
async def test_member_resolution_failures_name_their_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str | None, error: Exception | None, cause: str
) -> None:
    env = await _setup(tmp_path, monkeypatch)
    env.client.member = member
    env.client.member_error = error
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == cause
    if cause == "owner_not_member":
        assert caught.value.workspace == "Retail"


async def test_no_selected_workspace_names_its_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch)

    async def _none(state: Any, datarc_client: Any, user_id: str | None, **kwargs: Any) -> tuple[None, str, tuple[dict, ...], str]:
        return None, "", (), "zero_workspaces"

    monkeypatch.setattr(datarc_context, "_resolve_per_user_datarc_workspace", _none)
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == "no_workspace"


async def test_a_rejected_key_during_the_workspace_lookup_is_a_rejected_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await _setup(tmp_path, monkeypatch)

    async def _rejecting(state: Any, datarc_client: Any, user_id: str | None, **kwargs: Any) -> Any:
        # The ladder's Datarc read fails on the key; read-only mode then reports a transient snapshot.
        with contextlib.suppress(BIPermissionError):
            await datarc_client.fetch_workspace_member(datarc_workspace_id="0", username="x")
        raise RuntimeError("Datarc context snapshot is unavailable")

    env.client.member_error = BIPermissionError("datarc 401: access rejected", source="http")
    monkeypatch.setattr(datarc_context, "_resolve_per_user_datarc_workspace", _rejecting)
    with pytest.raises(EvalDatarcUnavailable) as caught:
        await TeamOwnerDatarcResolver(db_path=env.db_path, state=env.state).resolve(_run(env, created_by=env.owner.id))
    assert caught.value.cause == "connection_rejected"
