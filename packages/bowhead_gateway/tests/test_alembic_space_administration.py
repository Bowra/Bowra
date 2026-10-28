from __future__ import annotations

import sqlite3

import anyio
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.user_store import UserStore

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA index_list({table})").fetchall()}


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
            ("team_legacy", "Legacy Team", "active", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (
                "ws_legacy",
                "team_legacy",
                "Legacy Space",
                "active",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO team_invite_tokens(id,team_id,team_role,invited_email,token_hash,status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "oinv_legacy",
                "team_legacy",
                "member",
                "legacy@example.com",
                "legacy-token-hash",
                "active",
                "2026-01-01T00:00:00+00:00",
                "2999-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO scheduled_jobs(id,user_id,workspace_id,name,cron,query,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "sj_legacy",
                "u_legacy",
                "ws_legacy",
                "Legacy Job",
                "0 9 * * *",
                "summarize",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()


def test_space_administration_upgrades_1_0_core_schema(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    assert upgrade(db_url, target="core") == ("core",)
    assert upgrade(db_url, target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert {"name_key", "revision"} <= _column_names(connection, "workspaces")
        assert {
            "assignment_mode",
            "assignment_revision",
            "public_entry_id",
        } <= _column_names(connection, "team_invite_tokens")
        assert "pause_reason" in _column_names(connection, "scheduled_jobs")
        assert connection.execute(
            "SELECT assignment_mode,assignment_revision,public_entry_id FROM team_invite_tokens WHERE id='oinv_legacy'"
        ).fetchone() == ("team_only_legacy", 1, None)
        assert connection.execute("SELECT name_key,revision FROM workspaces WHERE id='ws_legacy'").fetchone() == (None, 1)
        assert connection.execute("SELECT pause_reason FROM scheduled_jobs WHERE id='sj_legacy'").fetchone() == (None,)
        assert {
            "ix_workspaces_team_status_name",
        } <= _index_names(connection, "workspaces")
        assert {
            "ix_team_invite_workspace_grants_workspace",
        } <= _index_names(connection, "team_invite_workspace_grants")
        assert {
            "idx_scheduled_jobs_workspace_status",
        } <= _index_names(connection, "scheduled_jobs")
        assert {
            "artifact_id",
            "workspace_id",
            "user_id",
            "session_id",
            "created_at",
        } <= _column_names(connection, "artifact_upload_bindings")
        assert {
            "idx_artifact_upload_bindings_workspace",
        } <= _index_names(connection, "artifact_upload_bindings")

    workspace = anyio.run(UserStore(db_path).get_workspace, "ws_legacy")
    invite = anyio.run(
        UserStore(db_path).get_team_invite_by_id,
        "oinv_legacy",
    )
    job = anyio.run(ScheduledJobStore(str(db_path)).get, "sj_legacy")

    assert workspace is not None
    assert workspace["revision"] == 1
    assert invite is not None
    assert invite["assignment_mode"] == "team_only_legacy"
    assert invite["initial_workspace_grants"] == []
    assert job is not None
    assert job.pause_reason is None
