"""Scoped workspace choices, original ladder and transaction failure boundaries."""

from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.datarc_selection import DatarcPreferenceScope
from bowhead_core.user_memory import UserMemoryStore
from bowhead_gateway.connector_context_provider import DefaultContextLadder, commit_connector_context_choice
from bowhead_gateway.connector_context_service import DatarcWorkspaceRef
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

SCOPE = DatarcPreferenceScope("team_one", "dc_one")
REFS = tuple(DatarcWorkspaceRef(str(i), "Source " + str(i)) for i in (1, 2, 3))
MIGRATION = import_module("bowhead_core.migrations.versions.0205_datarc_context")


@pytest.fixture
async def store(tmp_path):
    path = tmp_path / "memory.db"
    _upgrade(path)
    return UserMemoryStore(path)


async def choose(store, identifier, *, user="usr_actor", session="s_one", remember=False, scope=SCOPE):
    return await commit_connector_context_choice(
        store,
        user_id=user,
        tool="datarc",
        context_id=identifier,
        remember=remember,
        session_id=session,
        preference_scope=scope,
    )


async def resolve(store, *, user="usr_actor", session="s_one", scope=SCOPE):
    return await DefaultContextLadder(tool="datarc", umem=store, preference_scope=scope).resolve(
        user,
        session_id=session,
        accessible=REFS,
        read_only=True,
    )


async def test_two_chats_and_remember_preserve_the_other_overlay(store):
    assert await choose(store, "1", remember=True)
    assert await choose(store, "2", session="s_one")
    assert await choose(store, "3", session="s_two")
    assert (await resolve(store)).context_id == "2"
    assert (await resolve(store, session="s_two")).context_id == "3"
    assert await choose(store, "1", remember=True)
    assert await store.get_datarc_workspace("usr_actor", SCOPE, session_id="s_one") is None
    assert (await resolve(store, session="s_two")).context_id == "3"


async def test_default_and_overlay_rollback_together(store, monkeypatch):
    assert await choose(store, "1", remember=True)
    assert await choose(store, "3")

    async def fail_after_default(*args):
        raise RuntimeError("Controlled overlay cleanup failure")

    monkeypatch.setattr(store, "_clear_datarc_workspace_in_connection", fail_after_default)
    outcome = await choose(store, "2", remember=True)
    assert outcome.status == "failed"
    assert (await store.get_datarc_workspace("usr_actor", SCOPE))["id"] == "1"
    assert (await resolve(store)).context_id == "3"


async def test_commit_acknowledgement_loss_reports_unknown_instead_of_rollback(store, monkeypatch):
    import aiosqlite

    original = aiosqlite.Connection.commit

    async def commit_then_lose_acknowledgement(connection):
        await original(connection)
        raise OSError("Controlled acknowledgement loss after commit")

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_then_lose_acknowledgement)
    outcome = await choose(store, "2")
    assert outcome.status == "unknown"
    assert (await store.get_datarc_workspace("usr_actor", SCOPE, session_id="s_one"))["id"] == "2"


@pytest.mark.parametrize(
    "user,scope",
    [
        ("usr_other", SCOPE),
        ("usr_actor", DatarcPreferenceScope("team_two", "dc_one")),
        ("usr_actor", DatarcPreferenceScope("team_one", "dc_two")),
    ],
)
async def test_overlapping_external_ids_do_not_cross_user_team_or_connection(store, user, scope):
    assert await choose(store, "2")
    assert (await resolve(store, user=user, scope=scope)).context_id is None


async def test_legacy_values_never_revive_and_account_slot_is_unchanged(store):
    await store.set_tool_context("usr_actor", "datarc", context_id="3", source="explicit")
    await store.set_tool_context("usr_actor", "datarc", context_id="2", source="session", session_id="s_one")
    await store.set_tool_context("usr_actor", "datarc", slot="account", context_id="account_one")
    assert (await resolve(store)).context_id is None
    assert await choose(store, "1", remember=True)
    assert (await resolve(store)).context_id == "1"
    assert (await store.get_tool_context("usr_actor", "datarc"))["id"] == "3"
    assert (await store.get_tool_context("usr_actor", "datarc", slot="account"))["id"] == "account_one"


async def test_missing_session_never_writes_overlay(store):
    receipt = await choose(store, "2", session=None)
    assert receipt.status == "skipped" and receipt.reason == "missing_session"
    assert await store.get_datarc_workspace("usr_actor", SCOPE) is None


async def test_unmigrated_writer_refuses_new_values(tmp_path):
    path = tmp_path / "old.db"
    _upgrade(path, MIGRATION.down_revision)
    store = UserMemoryStore(path)
    assert (await choose(store, "2")).status == "failed"
    assert await store.get_datarc_workspace("usr_actor", SCOPE, session_id="s_one") is None


@pytest.mark.parametrize("operation", ["put", "update"])
async def test_unmigrated_clarification_writer_refuses_source_evidence(tmp_path, operation):
    from bowhead_core.clarification_store import ClarificationCandidate, ClarificationStore
    from bowhead_core.datarc_selection import DatarcSelectionSnapshot

    path = tmp_path / "old.db"
    _upgrade(path, MIGRATION.down_revision)
    store = ClarificationStore(path)
    context = {
        "datarc_selection": DatarcSelectionSnapshot(
            state="selected",
            team_id="team_one",
            user_id="usr_actor",
            connection_id="dc_one",
            connection_revision="revision_one",
            datarc_workspace_id="1",
            member_uid="member_one",
        ).model_dump(mode="json")
    }
    request = {
        "turn_id": "t_one",
        "user_id": "usr_actor",
        "workspace_id": "ws_one",
        "token": "",
        "rationale": "Choose a workspace",
        "candidates": [ClarificationCandidate(value="1", label="Source A")],
    }
    if operation == "update":
        card = await store.put(**request)
        with pytest.raises(ValueError, match="datarc_context_migration_required"):
            await store.set_resume_context(card.request_id, context)
        assert (await store.get(card.request_id)).resume_context is None
    else:
        with pytest.raises(ValueError, match="datarc_context_migration_required"):
            await store.put(**request, resume_context=context)
        assert await store.get_pending_for_turn("t_one") == []


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_migration_preserves_all_rows_and_schema_and_is_repeatable(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, ddl = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    assert schema(path) == ddl
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
