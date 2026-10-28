"""Immutable team-branding draft copies.

Revision ID: 0041_team_branding_draft_copies
Revises: 0040_agent_eval_product
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0041_team_branding_draft_copies"
down_revision = "0040_agent_eval_product"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "team_branding_draft_copies",
        """
        CREATE TABLE team_branding_draft_copies (
          id                         TEXT PRIMARY KEY CHECK(id LIKE 'tbc_%'),
          team_id                    TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          source_draft_version       INTEGER NOT NULL CHECK(source_draft_version >= 1),
          original_base_revision_id  TEXT NOT NULL
                                        REFERENCES team_branding_revisions(id) ON DELETE RESTRICT,
          schema_version             INTEGER NOT NULL,
          source_json                TEXT NOT NULL,
          compiled_json              TEXT,
          compile_status             TEXT NOT NULL
                                        CHECK(compile_status IN ('valid','invalid','unavailable')),
          defaults_json              TEXT NOT NULL,
          behavior                   TEXT NOT NULL CHECK(behavior IN ('default','enforced')),
          application_name           TEXT NOT NULL,
          logo_alt                   TEXT,
          logo_light_artifact_id     TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
          logo_dark_artifact_id      TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
          mark_artifact_id           TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
          favicon_artifact_id        TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
          release_note               TEXT,
          generator_id               TEXT NOT NULL,
          generator_version          TEXT NOT NULL,
          validation_json            TEXT NOT NULL,
          checksum                   TEXT NOT NULL,
          source_updated_by          TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          source_updated_at          TEXT NOT NULL,
          preserved_by               TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          preserved_at               TEXT NOT NULL,
          CHECK(
            (compile_status='valid' AND compiled_json IS NOT NULL)
            OR (compile_status IN ('invalid','unavailable') AND compiled_json IS NULL)
          )
        )
        """,
    ),
    (
        "index",
        "idx_team_branding_draft_copies_team",
        """
        CREATE INDEX idx_team_branding_draft_copies_team
        ON team_branding_draft_copies(team_id, preserved_at DESC, id DESC)
        """,
    ),
    (
        "trigger",
        "trg_team_branding_draft_copy_scope",
        """
        CREATE TRIGGER trg_team_branding_draft_copy_scope
        BEFORE INSERT ON team_branding_draft_copies
        WHEN NOT EXISTS (
          SELECT 1 FROM team_branding_revisions revision
          WHERE revision.id=NEW.original_base_revision_id
            AND revision.team_id=NEW.team_id
        ) OR (
          NEW.logo_light_artifact_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM team_brand_asset_metadata asset
            WHERE asset.artifact_id=NEW.logo_light_artifact_id
              AND asset.team_id=NEW.team_id
          )
        ) OR (
          NEW.logo_dark_artifact_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM team_brand_asset_metadata asset
            WHERE asset.artifact_id=NEW.logo_dark_artifact_id
              AND asset.team_id=NEW.team_id
          )
        ) OR (
          NEW.mark_artifact_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM team_brand_asset_metadata asset
            WHERE asset.artifact_id=NEW.mark_artifact_id
              AND asset.team_id=NEW.team_id
          )
        ) OR (
          NEW.favicon_artifact_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM team_brand_asset_metadata asset
            WHERE asset.artifact_id=NEW.favicon_artifact_id
              AND asset.team_id=NEW.team_id
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'team branding draft copy scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_branding_draft_copy_no_update",
        """
        CREATE TRIGGER trg_team_branding_draft_copy_no_update
        BEFORE UPDATE ON team_branding_draft_copies
        BEGIN
          SELECT RAISE(ABORT, 'team branding draft copies are immutable');
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


def _ensure_object(connection: Any, *, object_type: str, name: str, ddl: str) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(f"0041 found incompatible {object_type} semantics: {name}")


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
