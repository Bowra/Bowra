"""Shared catalog localization envelope for Agent, Skill, and Capability.

The revision is expand-only and does not translate or backfill business data.
Product writes remain behind FEATURE_AGENT_INTELLIGENCE_M4.

Revision ID: 0042_catalog_localization
Revises: 0041_team_branding_draft_copies
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0042_catalog_localization"
down_revision = "0041_team_branding_draft_copies"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "catalog_localized_listing",
        """
        CREATE TABLE catalog_localized_listing (
          id                TEXT PRIMARY KEY CHECK(id LIKE 'cll_%'),
          team_id           TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          resource_kind     TEXT NOT NULL CHECK(resource_kind IN ('agent','skill','capability')),
          resource_id       TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 240),
          resource_revision TEXT NOT NULL CHECK(length(resource_revision) BETWEEN 1 AND 240),
          locale            TEXT NOT NULL CHECK(length(locale) BETWEEN 2 AND 40),
          source_locale     TEXT NOT NULL CHECK(length(source_locale) BETWEEN 2 AND 40),
          fields_json       TEXT NOT NULL CHECK(length(fields_json) BETWEEN 2 AND 32000),
          source_hash       TEXT NOT NULL CHECK(source_hash LIKE 'sha256:%'),
          generation_source TEXT NOT NULL CHECK(generation_source IN ('manual','llm','platform')),
          review_state      TEXT NOT NULL CHECK(review_state IN ('draft','confirmed','stale')),
          row_version       INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          reviewed_by       TEXT REFERENCES users(id) ON DELETE RESTRICT,
          reviewed_at       TEXT,
          created_by        TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at        TEXT NOT NULL,
          updated_at        TEXT NOT NULL,
          UNIQUE(team_id,resource_kind,resource_id,resource_revision,locale),
          CHECK(locale<>source_locale),
          CHECK(
            (review_state='draft' AND reviewed_by IS NULL AND reviewed_at IS NULL)
            OR (review_state='confirmed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
            OR (review_state='stale'
                AND ((reviewed_by IS NULL AND reviewed_at IS NULL)
                     OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)))
          )
        )
        """,
    ),
    (
        "index",
        "idx_catalog_localized_listing_lookup",
        """
        CREATE INDEX idx_catalog_localized_listing_lookup
        ON catalog_localized_listing(
          team_id,resource_kind,resource_id,resource_revision,review_state,locale
        )
        """,
    ),
    (
        "index",
        "idx_agent_name_claim_effective",
        """
        CREATE INDEX idx_agent_name_claim_effective
        ON agent_name_claim(team_id,normalized_key,locale)
        """,
    ),
    (
        "trigger",
        "trg_catalog_localized_listing_scope",
        """
        CREATE TRIGGER trg_catalog_localized_listing_scope
        BEFORE INSERT ON catalog_localized_listing
        WHEN NOT EXISTS (
          SELECT 1
          FROM teams team
          JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=NEW.team_id AND member.user_id=actor.id
           AND member.status='active'
          WHERE team.id=NEW.team_id AND team.status='active'
        ) OR (
          NEW.resource_kind='agent' AND NOT EXISTS (
            SELECT 1
            FROM team_subagent_working_copy working
            JOIN team_subagent agent
              ON agent.team_id=working.team_id AND agent.id=working.subagent_id
            WHERE working.team_id=NEW.team_id
              AND working.subagent_id=NEW.resource_id
              AND working.id=NEW.resource_revision
              AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.created_by
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'catalog localization scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_catalog_localized_listing_update",
        """
        CREATE TRIGGER trg_catalog_localized_listing_update
        BEFORE UPDATE ON catalog_localized_listing
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.resource_kind IS NOT OLD.resource_kind
          OR NEW.resource_id IS NOT OLD.resource_id
          OR NEW.resource_revision IS NOT OLD.resource_revision
          OR NEW.locale IS NOT OLD.locale
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version != OLD.row_version+1
          OR NEW.updated_at <= OLD.updated_at
          OR (
            OLD.review_state='confirmed' AND NOT (
              NEW.review_state='draft'
              OR (
                NEW.review_state IN ('confirmed','stale')
                AND NEW.source_locale IS OLD.source_locale
                AND NEW.fields_json IS OLD.fields_json
                AND NEW.source_hash IS OLD.source_hash
                AND NEW.generation_source IS OLD.generation_source
                AND NEW.reviewed_by IS OLD.reviewed_by
                AND NEW.reviewed_at IS OLD.reviewed_at
              )
            )
          )
          OR (
            OLD.review_state='stale' AND NEW.review_state NOT IN ('stale','draft')
          )
        BEGIN
          SELECT RAISE(ABORT, 'catalog localization update mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_catalog_localized_listing_delete",
        """
        CREATE TRIGGER trg_catalog_localized_listing_delete
        BEFORE DELETE ON catalog_localized_listing
        WHEN OLD.review_state!='draft'
        BEGIN
          SELECT RAISE(ABORT, 'confirmed catalog localization cannot be deleted');
        END
        """,
    ),
)


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _object_sql(connection: Any, *, object_type: str, name: str) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _ensure_object(
    connection: Any,
    *,
    object_type: str,
    name: str,
    ddl: str,
) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(
            f"0042 found incompatible {object_type} semantics: {name}",
        )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(
            connection,
            object_type=object_type,
            name=name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
