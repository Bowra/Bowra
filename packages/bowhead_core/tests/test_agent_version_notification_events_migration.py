"""The Owner notification outbox admits proposal events without changing historical rows."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0182_agent_version_notification_events")
TIME = "2026-10-18T00:00:00+00:00"


def _seed_outbox(path) -> None:
    _seed(path)
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO agent_version_notification_outbox(id,team_id,agent_id,recipient_user_id,event_type,
                   release_id,dedupe_key,safe_payload_json,status,settled_at,available_at,created_at,outcome_code)
               SELECT 'aevn_old','team_release','osa_release','user_owner','release.regression',id,
                      'release.regression:old','{"schema_version":1}','delivered',?,?,?,'historical'
               FROM team_subagent_release WHERE team_id='team_release' LIMIT 1""",
            (TIME, TIME, TIME),
        )
        if db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox").fetchone()[0] != 1:
            raise AssertionError("seed needs one historical outbox row")


def _insert(db, row_id, event_type, payload, *, recipient="user_owner"):
    db.execute(
        """INSERT INTO agent_version_notification_outbox(id,team_id,agent_id,recipient_user_id,event_type,
               dedupe_key,safe_payload_json,status,available_at,created_at)
           VALUES(?,'team_release','osa_release',?,?,?,?,'pending',?,?)""",
        (row_id, recipient, event_type, f"{event_type}:{row_id}", payload, TIME, TIME),
    )


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_rebuild_preserves_rows_and_admits_proposal_events(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed_outbox(path)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        if target != "core":
            return
        assert db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox").fetchone()[0] == 1
        triggers = {
            row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='agent_version_notification_outbox'")
        }
        assert {
            "trg_agent_version_notification_recipient_scope",
            "trg_agent_version_notification_payload_immutable",
            "trg_agent_version_notification_immutable_delete",
            "trg_account_retired_agent_version_notification_outbox_insert",
        } <= triggers
        _insert(db, "aevn_proposal", "proposal.created", '{"proposal_id":"acp_1","schema_version":1}')
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(db, "aevn_unknown", "proposal.exploded", '{"proposal_id":"acp_2"}')
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            # A proposal event names its proposal; other events still need a version identity.
            _insert(db, "aevn_no_proposal", "proposal.created", '{"schema_version":1}')
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(db, "aevn_no_identity", "candidate.ready", '{"proposal_id":"acp_3"}')
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_version_notification_outbox SET event_type='candidate.ready' WHERE id='aevn_old'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM agent_version_notification_outbox WHERE id='aevn_old'")
        # An Owner whose home Team is another Team is still in scope through membership.
        db.execute("INSERT INTO teams(id,name,status,created_by_user_id,created_at) VALUES('team_home','Home','active','user_admin',?)", (TIME,))
        db.execute("INSERT INTO users(id,team_id,email,status,created_at) VALUES('user_guest','team_home','g@example.com','active',?)", (TIME,))
        with pytest.raises(sqlite3.IntegrityError, match="recipient scope"):
            _insert(db, "aevn_outsider", "proposal.created", '{"proposal_id":"acp_4"}', recipient="user_guest")
        db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES('tm_guest','user_guest','team_release','member','active',?)",
            (TIME,),
        )
        _insert(db, "aevn_member", "proposal.created", '{"proposal_id":"acp_4"}', recipient="user_guest")


def test_rows_pending_at_upgrade_are_settled_as_backfill(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    _seed_outbox(path)
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO agent_version_notification_outbox(id,team_id,agent_id,recipient_user_id,event_type,
                   release_id,dedupe_key,safe_payload_json,status,available_at,created_at)
               SELECT 'aevn_backlog','team_release','osa_release','user_owner','release.regression',id,
                      'release.regression:backlog','{"schema_version":1}','pending',?,?
               FROM team_subagent_release WHERE team_id='team_release' LIMIT 1""",
            (TIME, TIME),
        )
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        rows = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT id,status,outcome_code,settled_at IS NOT NULL,attempt_count,lease_generation,dedupe_key "
                "FROM agent_version_notification_outbox"
            )
        }
    # The historical backlog is settled, never delivered; already settled rows keep their outcome.
    assert rows == {
        "aevn_old": ("delivered", "historical", 1, 0, 0, "release.regression:old"),
        "aevn_backlog": ("delivered", "backfill_skipped", 1, 0, 0, "release.regression:backlog"),
    }


def test_unknown_table_shape_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    monkeypatch.setattr(MIGRATION, "_EXPECTED_DDL", MIGRATION._EXPECTED_DDL.replace("'release.regression'", "'release.other'"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}),
                get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute),
            ),
        )
        with pytest.raises(RuntimeError, match="0073 schema"):
            MIGRATION.upgrade()


def test_partial_rebuild_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    _seed_outbox(path)
    before_schema, before_rows = schema(path), snapshot(path)
    monkeypatch.setattr(MIGRATION, "_RECIPIENT_TRIGGER_SQL", "invalid sql")
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}),
                get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute),
            ),
        )
        with pytest.raises(sqlite3.OperationalError):
            MIGRATION.upgrade()
    assert schema(path) == before_schema and snapshot(path) == before_rows
