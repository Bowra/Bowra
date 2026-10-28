"""Add immutable behavior descriptor sidecars for exact Agent revisions.

The migration is expand-only and intentionally leaves existing revisions
without rows.  A later explicit offline backfill owns legacy descriptor
materialization; migrations never run behavior compilers.

Revision ID: 0050_agent_behavior_descriptor
Revises: 0049_chart_style_customization
Create Date: 2026-09-06
"""

from __future__ import annotations

from alembic import context

revision = "0050_agent_behavior_descriptor"
down_revision = "0049_chart_style_customization"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "team_subagent_revision_behavior",
        """
        CREATE TABLE team_subagent_revision_behavior (
          revision_id           TEXT PRIMARY KEY CHECK(substr(revision_id,1,4)='arv_'),
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subagent_id           TEXT NOT NULL,
          source_definition_hash TEXT NOT NULL
            CHECK(length(source_definition_hash)=71 AND substr(source_definition_hash,1,7)='sha256:'
              AND substr(source_definition_hash,8) NOT GLOB '*[^0-9a-f]*'),
          descriptor_json       TEXT NOT NULL CHECK(
            CASE
              WHEN length(descriptor_json) BETWEEN 2 AND 262144
                AND json_valid(descriptor_json) AND json_type(descriptor_json)='object'
              THEN COALESCE(
                json_type(descriptor_json,'$.schema_version')='integer'
                AND json_extract(descriptor_json,'$.schema_version')=1
                AND json_type(descriptor_json,'$.source_definition_hash')='text'
                AND json_extract(descriptor_json,'$.source_definition_hash')=source_definition_hash,
                0
              )
              ELSE 0
            END
          ),
          behavior_fingerprint  TEXT NOT NULL
            CHECK(length(behavior_fingerprint)=71 AND substr(behavior_fingerprint,1,7)='sha256:'
              AND substr(behavior_fingerprint,8) NOT GLOB '*[^0-9a-f]*'
              AND COALESCE(json_type(descriptor_json,'$.behavior_fingerprint')='text'
                AND json_extract(descriptor_json,'$.behavior_fingerprint')=behavior_fingerprint,0)),
          compiler_version      TEXT NOT NULL
            CHECK(length(compiler_version) BETWEEN 1 AND 120
              AND COALESCE(json_type(descriptor_json,'$.compiler_version')='text'
                AND json_extract(descriptor_json,'$.compiler_version')=compiler_version,0)),
          created_at            TEXT NOT NULL CHECK(length(created_at)>0),
          FOREIGN KEY(team_id,subagent_id,revision_id)
            REFERENCES team_subagent_revision(team_id,subagent_id,id) ON DELETE RESTRICT
        ) WITHOUT ROWID
        """,
    ),
    (
        "table",
        "system_agent_revision_behavior",
        """
        CREATE TABLE system_agent_revision_behavior (
          revision_id           TEXT PRIMARY KEY CHECK(substr(revision_id,1,4)='arv_'),
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_id              TEXT NOT NULL CHECK(agent_id LIKE 'system:%'),
          source_definition_hash TEXT NOT NULL
            CHECK(length(source_definition_hash)=71 AND substr(source_definition_hash,1,7)='sha256:'
              AND substr(source_definition_hash,8) NOT GLOB '*[^0-9a-f]*'),
          descriptor_json       TEXT NOT NULL CHECK(
            CASE
              WHEN length(descriptor_json) BETWEEN 2 AND 262144
                AND json_valid(descriptor_json) AND json_type(descriptor_json)='object'
              THEN COALESCE(
                json_type(descriptor_json,'$.schema_version')='integer'
                AND json_extract(descriptor_json,'$.schema_version')=1
                AND json_type(descriptor_json,'$.source_definition_hash')='text'
                AND json_extract(descriptor_json,'$.source_definition_hash')=source_definition_hash,
                0
              )
              ELSE 0
            END
          ),
          behavior_fingerprint  TEXT NOT NULL
            CHECK(length(behavior_fingerprint)=71 AND substr(behavior_fingerprint,1,7)='sha256:'
              AND substr(behavior_fingerprint,8) NOT GLOB '*[^0-9a-f]*'
              AND COALESCE(json_type(descriptor_json,'$.behavior_fingerprint')='text'
                AND json_extract(descriptor_json,'$.behavior_fingerprint')=behavior_fingerprint,0)),
          compiler_version      TEXT NOT NULL
            CHECK(length(compiler_version) BETWEEN 1 AND 120
              AND COALESCE(json_type(descriptor_json,'$.compiler_version')='text'
                AND json_extract(descriptor_json,'$.compiler_version')=compiler_version,0)),
          created_at            TEXT NOT NULL CHECK(length(created_at)>0),
          FOREIGN KEY(team_id,agent_id,revision_id)
            REFERENCES system_agent_effective_revision(team_id,agent_id,id) ON DELETE RESTRICT
        ) WITHOUT ROWID
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_revision_behavior_insert",
        """
        CREATE TRIGGER trg_team_subagent_revision_behavior_insert
        BEFORE INSERT ON team_subagent_revision_behavior
        WHEN NOT EXISTS (
          SELECT 1 FROM team_subagent_revision revision
          WHERE revision.team_id=NEW.team_id
            AND revision.subagent_id=NEW.subagent_id
            AND revision.id=NEW.revision_id
            AND revision.definition_hash=NEW.source_definition_hash
        )
        BEGIN
          SELECT RAISE(ABORT, 'team Agent behavior source mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_system_agent_revision_behavior_insert",
        """
        CREATE TRIGGER trg_system_agent_revision_behavior_insert
        BEFORE INSERT ON system_agent_revision_behavior
        WHEN NOT EXISTS (
          SELECT 1 FROM system_agent_effective_revision revision
          WHERE revision.team_id=NEW.team_id
            AND revision.agent_id=NEW.agent_id
            AND revision.id=NEW.revision_id
            AND revision.definition_hash=NEW.source_definition_hash
        )
        BEGIN
          SELECT RAISE(ABORT, 'system Agent behavior source mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_revision_behavior_immutable_update",
        """
        CREATE TRIGGER trg_team_subagent_revision_behavior_immutable_update
        BEFORE UPDATE ON team_subagent_revision_behavior
        BEGIN
          SELECT RAISE(ABORT, 'team Agent behavior is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_revision_behavior_immutable_delete",
        """
        CREATE TRIGGER trg_team_subagent_revision_behavior_immutable_delete
        BEFORE DELETE ON team_subagent_revision_behavior
        BEGIN
          SELECT RAISE(ABORT, 'team Agent behavior is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_system_agent_revision_behavior_immutable_update",
        """
        CREATE TRIGGER trg_system_agent_revision_behavior_immutable_update
        BEFORE UPDATE ON system_agent_revision_behavior
        BEGIN
          SELECT RAISE(ABORT, 'system Agent behavior is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_system_agent_revision_behavior_immutable_delete",
        """
        CREATE TRIGGER trg_system_agent_revision_behavior_immutable_delete
        BEFORE DELETE ON system_agent_revision_behavior
        BEGIN
          SELECT RAISE(ABORT, 'system Agent behavior is immutable');
        END
        """,
    ),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for kind, name, ddl in _OBJECTS:
        current = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
            (kind, name),
        ).scalar()
        if current is None:
            connection.exec_driver_sql(ddl)
        elif _normalized_sql(current) != _normalized_sql(ddl):
            raise RuntimeError(f"Agent behavior descriptor schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain immutable Agent behavior descriptors.")


def _normalized_sql(value: str) -> str:
    return "".join(value.lower().split())
