"""Released defaults retain attributable sources through the native upgrade."""

from __future__ import annotations

import json
import sqlite3
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.datarc_selection import DatarcPreferenceScope
from bowhead_core.user_memory import UserMemoryStore
from bowhead_gateway.connector_context_provider import DefaultContextLadder
from bowhead_gateway.connector_context_service import DatarcWorkspaceRef
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

MIGRATION = import_module("bowhead_core.migrations.versions.0210_datarc_preference_backfill")
EARLY = "2026-01-01T00:00:00+00:00"
CHOICE = "2026-02-01T00:00:00+00:00"
LATER = "2026-03-01T00:00:00+00:00"
SCOPE = DatarcPreferenceScope("team_one", "dc_one")
pytestmark = pytest.mark.integration


def _memory(db: sqlite3.Connection, identifier: str, key: str, value: object, *, deleted: bool = False) -> None:
    db.execute(
        "INSERT INTO memory_entries(id,subject_type,subject_id,kind,key,value,source,created_at,updated_at,deleted_at) "
        "VALUES (?,'user','usr_one','tool_context',?,?,'tool_runtime',?,?,?)",
        (identifier, key, json.dumps(value), CHOICE, CHOICE, LATER if deleted else None),
    )


def _choice(*, source: str = "explicit", session: str | None = None, identifier: str = "1") -> dict[str, Any]:
    return {"id": identifier, "name": "Legacy source", "source": source, "updated_at": CHOICE, **({"session_id": session} if session else {})}


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    path = tmp_path / "bowhead.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_one','Team',?)", (EARLY,))
        db.execute("INSERT INTO users(id,team_id,email,status,created_at) VALUES ('usr_one','team_one','actor@example.test','active',?)", (EARLY,))
        db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES ('om_one','usr_one','team_one','member','active',?)",
            (EARLY,),
        )
        db.execute(
            "INSERT INTO datarc_connection(id,team_id,name,base_url,status,created_at,updated_at) "
            "VALUES ('dc_one','team_one','Source','https://source.example','active',?,?)",
            (EARLY, EARLY),
        )
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES ('ws_one','team_one','Space',?,?)", (EARLY, EARLY))
        db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at) VALUES ('s_one','ws_one','usr_one','Chat',?,?)",
            (EARLY, EARLY),
        )
        _memory(db, "mem_old", "datarc/workspace", _choice())
    return path


def _entries(path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM memory_entries ORDER BY id")]


async def test_upgrade_keeps_default_and_exact_chat_overlay_without_rewriting_history(legacy: Path) -> None:
    with sqlite3.connect(legacy) as db:
        _memory(db, "mem_session", "datarc/workspace@session", _choice(source="session", session="s_one", identifier="2"))
        _memory(db, "mem_account", "datarc/account", {"id": "account_one"})
    original, original_schema = _entries(legacy), schema(legacy)
    _upgrade(legacy, MIGRATION.revision)
    assert schema(legacy) == original_schema
    _upgrade(legacy)
    store = UserMemoryStore(legacy)
    default = await store.get_datarc_workspace("usr_one", SCOPE)
    overlay = await store.get_datarc_workspace("usr_one", SCOPE, session_id="s_one")
    assert default == {**_choice(), "schema_version": 2, "team_id": "team_one", "connection_id": "dc_one"}
    assert overlay == {
        **_choice(source="session", session="s_one", identifier="2"),
        "schema_version": 2,
        "team_id": "team_one",
        "connection_id": "dc_one",
    }
    refs = tuple(DatarcWorkspaceRef(str(i), "Current source " + str(i)) for i in (1, 2, 3))
    ladder = DefaultContextLadder(tool="datarc", umem=store, preference_scope=SCOPE)
    assert (await ladder.resolve("usr_one", session_id="s_one", accessible=refs, read_only=True)).context_id == "2"
    assert (await ladder.resolve("usr_one", session_id="another_chat", accessible=refs, read_only=True)).context_id == "1"
    assert await store.get_datarc_workspace("usr_other", SCOPE) is None
    assert await store.get_datarc_workspace("usr_one", DatarcPreferenceScope("team_other", "dc_one")) is None
    assert await store.get_datarc_workspace("usr_one", DatarcPreferenceScope("team_one", "dc_other")) is None
    after = _entries(legacy)
    assert [row for row in after if row["id"] in {entry["id"] for entry in original}] == original
    _upgrade(legacy)
    assert _entries(legacy) == after


@pytest.mark.parametrize("deleted", [False, True], ids=["new-default", "cleared-default"])
def test_existing_new_default_and_cleared_choices_take_precedence(legacy: Path, deleted: bool) -> None:
    with sqlite3.connect(legacy) as db:
        _memory(
            db,
            "mem_new",
            SCOPE.key(),
            {**_choice(identifier="3"), "schema_version": 2, "team_id": "team_one", "connection_id": "dc_one"},
            deleted=deleted,
        )
        _memory(db, "mem_session", "datarc/workspace@session", _choice(source="session", session="s_one", identifier="2"))
    before = _entries(legacy)
    _upgrade(legacy)
    assert _entries(legacy) == before


@pytest.mark.parametrize("deleted", [False, True], ids=["new-overlay", "cleared-overlay"])
def test_new_chat_overlay_does_not_replace_legacy_permanent_default(legacy: Path, deleted: bool) -> None:
    with sqlite3.connect(legacy) as db:
        _memory(
            db,
            "mem_new",
            SCOPE.key("s_one"),
            {**_choice(source="session", session="s_one", identifier="3"), "schema_version": 2, "team_id": "team_one", "connection_id": "dc_one"},
            deleted=deleted,
        )
        _memory(db, "mem_session", "datarc/workspace@session", _choice(source="session", session="s_one", identifier="2"))
    before = _entries(legacy)
    _upgrade(legacy)
    after = _entries(legacy)
    assert [row for row in after if row["key"] != SCOPE.key()] == before
    assert json.loads(next(row["value"] for row in after if row["key"] == SCOPE.key()))["id"] == "1"


@pytest.mark.parametrize(
    "condition",
    [
        "multiple_teams",
        "late_membership",
        "removed_membership",
        "late_connection",
        "replaced_connection",
        "ambiguous_connection",
        "unknown_connection_time",
        "deleted_legacy",
        "invalid_value",
        "missing_choice_time",
        "unknown_schema",
        "deletion_receipt",
        "conflicting_scope",
        "privacy_barrier",
    ],
)
def test_unproven_or_unavailable_legacy_default_stays_unselected(legacy: Path, condition: str) -> None:
    with sqlite3.connect(legacy) as db:
        if condition == "multiple_teams":
            db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_two','Other',?)", (EARLY,))
            db.execute("INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES ('om_two','usr_one','team_two','member',?)", (EARLY,))
        elif condition in {"late_membership", "removed_membership"}:
            if condition == "late_membership":
                db.execute("UPDATE team_members SET created_at=?", (LATER,))
            else:
                db.execute("UPDATE team_members SET status='removed',removed_at=?", (LATER,))
        elif condition in {"late_connection", "unknown_connection_time"}:
            db.execute("UPDATE datarc_connection SET created_at=?", (LATER if condition == "late_connection" else "unknown",))
        elif condition in {"replaced_connection", "ambiguous_connection"}:
            db.execute("UPDATE datarc_connection SET status='archived'")
            db.execute(
                "INSERT INTO datarc_connection(id,team_id,name,base_url,status,created_at,updated_at) VALUES ('dc_new','team_one','New','https://other.example','active',?,?)",
                (LATER if condition == "replaced_connection" else EARLY, LATER),
            )
        elif condition == "deleted_legacy":
            db.execute("UPDATE memory_entries SET deleted_at=?", (LATER,))
        elif condition == "invalid_value":
            db.execute("UPDATE memory_entries SET value='invalid JSON'")
        elif condition == "missing_choice_time":
            db.execute("UPDATE memory_entries SET value=?", (json.dumps({"id": "1", "source": "explicit"}),))
        elif condition == "unknown_schema":
            db.execute("UPDATE memory_entries SET value=?", (json.dumps({**_choice(), "schema_version": 99}),))
        elif condition == "deletion_receipt":
            db.execute(
                "INSERT INTO memory_resource_deletion_tombstones(user_id,resource_type,resource_id,deleted_through_version,deleted_at) "
                "VALUES ('usr_one','memory_entry','mem_old',1,?)",
                (LATER,),
            )
        elif condition == "conflicting_scope":
            db.execute("UPDATE memory_entries SET value=?", (json.dumps({**_choice(), "connection_id": "another_connection"}),))
        else:
            db.execute("INSERT INTO memory_privacy_state(user_id,barrier_status,updated_at) VALUES ('usr_one','active',?)", (LATER,))
    before = snapshot(legacy)
    _upgrade(legacy, MIGRATION.revision)
    assert snapshot(legacy) == before


@pytest.mark.parametrize("session", ["missing", "other_owner", "correct"], ids=["missing-chat", "foreign-chat", "original-chat"])
def test_session_source_is_bound_to_its_canonical_owned_chat(legacy: Path, session: str) -> None:
    with sqlite3.connect(legacy) as db:
        # A canonical Chat can identify its source even with two Team memberships.
        db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_two','Other',?)", (EARLY,))
        db.execute("INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES ('om_two','usr_one','team_two','member',?)", (EARLY,))
        _memory(
            db,
            "mem_session",
            "datarc/workspace@session",
            _choice(source="session", session="missing" if session == "missing" else "s_one", identifier="2"),
        )
        if session == "other_owner":
            db.execute("UPDATE sessions SET user_id='someone_else'")
    _upgrade(legacy)
    migrated = [row for row in _entries(legacy) if row["key"].startswith("datarc/workspace@v2/")]
    assert len(migrated) == (1 if session == "correct" else 0)
    if migrated:
        assert migrated[0]["key"] == SCOPE.key("s_one")


def test_upgrade_failure_rolls_back_preferences_and_ledger_before_retry(legacy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.engine import Connection

    with sqlite3.connect(legacy) as db:
        _memory(db, "mem_session", "datarc/workspace@session", _choice(source="session", session="s_one", identifier="2"))
    before = snapshot(legacy)
    original, inserts = Connection.exec_driver_sql, []

    def fail_second_insert(db: Connection, statement: str, *args: Any, **kwargs: Any) -> Any:
        if statement.startswith("INSERT INTO memory_entries"):
            inserts.append(statement)
            if len(inserts) == 2:
                raise RuntimeError("Controlled preference migration interruption")
        return original(db, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_second_insert)
    with pytest.raises(RuntimeError, match="Controlled preference migration interruption"):
        _upgrade(legacy)
    assert snapshot(legacy) == before
    with sqlite3.connect(legacy) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision
    monkeypatch.undo()
    _upgrade(legacy)
    assert len(_entries(legacy)) == 4


async def test_migrated_choice_is_still_checked_against_current_access(legacy: Path) -> None:
    _upgrade(legacy)
    store = UserMemoryStore(legacy)
    ladder = DefaultContextLadder(tool="datarc", umem=store, preference_scope=SCOPE)
    refs = tuple(DatarcWorkspaceRef(str(i), "Source " + str(i)) for i in (2, 3))
    assert (await ladder.resolve("usr_one", session_id="s_one", accessible=refs)).context_id is None
    assert await store.get_datarc_workspace("usr_one", SCOPE) is None
    assert (await store.get_tool_context("usr_one", "datarc"))["id"] == "1"
    assert (await ladder.resolve("usr_one", session_id="s_one", accessible=refs)).context_id is None


def test_legacy_session_key_retains_precedence_over_base_session_value(legacy: Path) -> None:
    with sqlite3.connect(legacy) as db:
        db.execute("UPDATE memory_entries SET value=? WHERE id='mem_old'", (json.dumps(_choice(source="session", session="s_one")),))
        _memory(db, "mem_session", "datarc/workspace@session", _choice(source="session", session="s_one", identifier="2"))
    _upgrade(legacy)
    migrated = [row for row in _entries(legacy) if row["key"].startswith("datarc/workspace@v2/")]
    assert len(migrated) == 1 and migrated[0]["key"] == SCOPE.key("s_one")
    assert json.loads(migrated[0]["value"])["id"] == "2"


async def test_automatic_choice_keeps_its_original_auto_semantics(legacy: Path) -> None:
    with sqlite3.connect(legacy) as db:
        db.execute("UPDATE memory_entries SET value=? WHERE id='mem_old'", (json.dumps(_choice(source="auto")),))
    _upgrade(legacy)
    store = UserMemoryStore(legacy)
    assert (await store.get_datarc_workspace("usr_one", SCOPE))["source"] == "auto"
    refs = tuple(DatarcWorkspaceRef(str(i), "Source " + str(i)) for i in (1, 2))
    resolved = await DefaultContextLadder(tool="datarc", umem=store, preference_scope=SCOPE).resolve("usr_one", session_id="s_one", accessible=refs)
    assert resolved.context_id is None


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_advance_without_schema_changes_and_repeat_safely(tmp_path: Path, target: str) -> None:
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, ddl = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    assert schema(path) == ddl
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
