from __future__ import annotations

import json
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

_BASELINE_REVISION = import_module("bowhead_core.migrations.versions.0048_document_inputs").revision

_DEFINITION_HASH = "sha256:" + "a" * 64
_BEHAVIOR_HASH = "sha256:" + "b" * 64
_DEPENDENCY_HASH = "sha256:" + "c" * 64
_CAPABILITY_HASH = "sha256:" + "d" * 64
_TIMESTAMP = "2026-09-06T00:00:00+00:00"


def _upgrade_to(db_path: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, revision)


def _insert_revision_fixtures(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO teams(id,name,status,created_at) VALUES ('team_behavior','Team','active',?)",
        (_TIMESTAMP,),
    )
    connection.execute(
        """
        INSERT INTO team_subagent(
          id,team_id,name,display_name,description,tool_refs_json,skills_json,
          visibility,status,created_at,updated_at
        ) VALUES (
          'osa_behavior','team_behavior','behavior','Behavior','Behavior fixture','[]','[]',
          'team_wide','enabled',?,?
        )
        """,
        (_TIMESTAMP, _TIMESTAMP),
    )
    connection.execute(
        """
        INSERT INTO team_subagent_revision(
          id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
          definition_hash,dependency_fingerprint,capability_fingerprint,
          risk_compiler_version,risk_json,created_by,created_at
        ) VALUES (
          'arv_behavior_team','team_behavior','osa_behavior',1,'authored','{}','{}',
          ?,?,?,'risk-v1','{}','user_behavior',?
        )
        """,
        (_DEFINITION_HASH, _DEPENDENCY_HASH, _CAPABILITY_HASH, _TIMESTAMP),
    )
    connection.execute(
        """
        INSERT INTO system_agent_effective_revision(
          id,team_id,agent_id,manifest_fingerprint,override_version,
          definition_json,definition_hash,created_at
        ) VALUES (
          'arv_behavior_system','team_behavior','system:compute',?,NULL,'{}',?,?
        )
        """,
        (_CAPABILITY_HASH, _DEFINITION_HASH, _TIMESTAMP),
    )


def _descriptor_json(*, source_hash: str = _DEFINITION_HASH) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "compiler_version": "agent-behavior-v1",
            "source_definition_hash": source_hash,
            "behavior_fingerprint": _BEHAVIOR_HASH,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_0050_adds_empty_immutable_behavior_sidecars(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, _BASELINE_REVISION)
    with sqlite3.connect(db_path) as connection:
        _insert_revision_fixtures(connection)
        connection.commit()

    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'",
            )
        }
        assert connection.execute("SELECT count(*) FROM team_subagent_revision_behavior").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM system_agent_revision_behavior").fetchone() == (0,)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)

    assert {"team_subagent_revision_behavior", "system_agent_revision_behavior"} <= tables
    assert {
        "trg_team_subagent_revision_behavior_insert",
        "trg_team_subagent_revision_behavior_immutable_update",
        "trg_team_subagent_revision_behavior_immutable_delete",
        "trg_system_agent_revision_behavior_insert",
        "trg_system_agent_revision_behavior_immutable_update",
        "trg_system_agent_revision_behavior_immutable_delete",
    } <= triggers


def test_0050_guards_source_identity_and_immutability(tmp_path: Path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_revision_fixtures(connection)
        incomplete_descriptor = json.dumps(
            {
                "schema_version": 1,
                "source_definition_hash": _DEFINITION_HASH,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_subagent_revision_behavior(
                  revision_id,team_id,subagent_id,source_definition_hash,descriptor_json,
                  behavior_fingerprint,compiler_version,created_at
                ) VALUES ('arv_behavior_team','team_behavior','osa_behavior',?,?,?,?,?)
                """,
                (
                    _DEFINITION_HASH,
                    incomplete_descriptor,
                    _BEHAVIOR_HASH,
                    "agent-behavior-v1",
                    _TIMESTAMP,
                ),
            )
        non_hex_behavior_hash = "sha256:" + "z" * 64
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_subagent_revision_behavior(
                  revision_id,team_id,subagent_id,source_definition_hash,descriptor_json,
                  behavior_fingerprint,compiler_version,created_at
                ) VALUES ('arv_behavior_team','team_behavior','osa_behavior',?,?,?,?,?)
                """,
                (
                    _DEFINITION_HASH,
                    _descriptor_json().replace(_BEHAVIOR_HASH, non_hex_behavior_hash),
                    non_hex_behavior_hash,
                    "agent-behavior-v1",
                    _TIMESTAMP,
                ),
            )
        connection.execute(
            """
            INSERT INTO team_subagent_revision_behavior(
              revision_id,team_id,subagent_id,source_definition_hash,descriptor_json,
              behavior_fingerprint,compiler_version,created_at
            ) VALUES ('arv_behavior_team','team_behavior','osa_behavior',?,?,?,?,?)
            """,
            (
                _DEFINITION_HASH,
                _descriptor_json(),
                _BEHAVIOR_HASH,
                "agent-behavior-v1",
                _TIMESTAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO system_agent_revision_behavior(
              revision_id,team_id,agent_id,source_definition_hash,descriptor_json,
              behavior_fingerprint,compiler_version,created_at
            ) VALUES ('arv_behavior_system','team_behavior','system:compute',?,?,?,?,?)
            """,
            (
                _DEFINITION_HASH,
                _descriptor_json(),
                _BEHAVIOR_HASH,
                "agent-behavior-v1",
                _TIMESTAMP,
            ),
        )

        wrong_hash = "sha256:" + "e" * 64
        with pytest.raises(sqlite3.IntegrityError, match="source mismatch"):
            connection.execute(
                """
                INSERT INTO system_agent_revision_behavior(
                  revision_id,team_id,agent_id,source_definition_hash,descriptor_json,
                  behavior_fingerprint,compiler_version,created_at
                ) VALUES ('arv_behavior_system','team_behavior','system:compute',?,?,?,?,?)
                """,
                (
                    wrong_hash,
                    _descriptor_json(source_hash=wrong_hash),
                    _BEHAVIOR_HASH,
                    "agent-behavior-v1",
                    _TIMESTAMP,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="team Agent behavior is immutable"):
            connection.execute(
                "UPDATE team_subagent_revision_behavior SET created_at=? WHERE revision_id='arv_behavior_team'",
                ("2026-09-06T01:00:00+00:00",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="system Agent behavior is immutable"):
            connection.execute(
                "DELETE FROM system_agent_revision_behavior WHERE revision_id='arv_behavior_system'",
            )

        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
