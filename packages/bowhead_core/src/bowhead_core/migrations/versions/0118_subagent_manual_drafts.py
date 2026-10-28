"""Expand the existing Subagent draft table for space-bound manual editing.

Revision ID: 0118_subagent_manual_drafts
Revises: 0117_working_copy_history_refs
Create Date: 2026-09-27
"""

from __future__ import annotations

import re

from alembic import context

revision = "0118_subagent_manual_drafts"
down_revision = "0117_working_copy_history_refs"
branch_labels = None
depends_on = None

_EXPECTED_COLUMNS = {
    "id", "team_id", "owner_user_id", "source_mode", "entry_mode",
    "source_turn_id", "status", "validation_status", "locale", "purpose_text",
    "goal_summary", "definition_json", "recommendations_json", "assumptions_json",
    "warnings_json", "clarification_json", "validation_issues_json",
    "catalog_fingerprint", "definition_fingerprint", "idempotency_key_hash",
    "mutation_id", "generation_attempt", "business_clarification_count",
    "row_version", "outcome_reason_code", "consumed_definition_id", "consumed_at",
    "discarded_at", "expired_at", "expires_at", "content_scrub_at", "purge_at",
    "created_at", "updated_at",
}
_INDEXES = (
    "CREATE UNIQUE INDEX uq_team_subagent_draft_idempotency "
    "ON team_subagent_draft(team_id, owner_user_id, idempotency_key_hash)",
    "CREATE INDEX idx_team_subagent_draft_owner_status_updated "
    "ON team_subagent_draft(team_id, owner_user_id, status, updated_at DESC)",
    "CREATE INDEX idx_team_subagent_draft_expiry "
    "ON team_subagent_draft(status, expires_at)",
    "CREATE INDEX idx_team_subagent_draft_source_turn "
    "ON team_subagent_draft(team_id, owner_user_id, source_turn_id) "
    "WHERE source_turn_id IS NOT NULL",
    "CREATE INDEX idx_team_subagent_draft_owner_space_status "
    "ON team_subagent_draft(team_id, owner_user_id, workspace_id, status, updated_at DESC)",
)


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("manual draft migration: source schema drift")
    return source.replace(old, new)


def _shadow_ddl(source: str) -> str:
    match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?team_subagent_draft\b",
        source,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError("manual draft migration: source table drift")
    result = source[:match.start()] + "CREATE TABLE team_subagent_draft_v2" + source[match.end():]
    result = _replace_once(result, "owner_user_id TEXT NOT NULL,", "owner_user_id TEXT NOT NULL,\n          workspace_id TEXT,")
    result = _replace_once(result, "workspace_id TEXT,", "workspace_id TEXT,\n          create_payload_hash TEXT,")
    result = _replace_once(
        result,
        "CHECK(source_mode IN ('intent', 'turn'))",
        "CHECK(source_mode IN ('intent', 'turn', 'manual'))",
    )
    result = _replace_once(
        result,
        "CHECK(entry_mode = 'conversational')",
        "CHECK(entry_mode IN ('conversational', 'canvas'))",
    )
    result = _replace_once(
        result,
        "'generating', 'needs_clarification', 'ready', 'blocked',",
        "'generating', 'needs_clarification', 'editing', 'ready', 'blocked',",
    )
    result = _replace_once(
        result,
        "OR (source_mode = 'intent' AND source_turn_id IS NULL)",
        "OR (source_mode IN ('intent', 'manual') AND source_turn_id IS NULL)",
    )
    result = _replace_once(
        result,
        "CHECK(length(goal_summary) <= 500),",
        "CHECK(length(goal_summary) <= 500),\n          CHECK(create_payload_hash IS NULL OR length(create_payload_hash) = 64),\n          CHECK(source_mode != 'manual' OR (workspace_id IS NOT NULL AND create_payload_hash IS NOT NULL)),",
    )
    return result


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    row = db.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='team_subagent_draft'",
    ).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("manual draft migration: source table missing")
    columns = [str(item[1]) for item in db.exec_driver_sql("PRAGMA table_info(team_subagent_draft)")]
    if set(columns) != _EXPECTED_COLUMNS:
        raise RuntimeError("manual draft migration: source columns drift")
    indexes = {
        str(item[0])
        for item in db.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='team_subagent_draft'",
        )
        if not str(item[0]).startswith("sqlite_autoindex_")
    }
    if indexes != {"uq_team_subagent_draft_idempotency", "idx_team_subagent_draft_owner_status_updated", "idx_team_subagent_draft_expiry", "idx_team_subagent_draft_source_turn"}:
        raise RuntimeError("manual draft migration: source indexes drift")
    shadow = _shadow_ddl(str(row[0]))
    names = ", ".join(columns)
    db.exec_driver_sql("SAVEPOINT subagent_manual_draft_expand")
    try:
        db.exec_driver_sql(shadow)
        db.exec_driver_sql(
            f"INSERT INTO team_subagent_draft_v2 ({names}, workspace_id) "
            f"SELECT {names}, NULL FROM team_subagent_draft",
        )
        count = db.exec_driver_sql("SELECT count(*) FROM team_subagent_draft").scalar_one()
        if count != db.exec_driver_sql("SELECT count(*) FROM team_subagent_draft_v2").scalar_one():
            raise RuntimeError("manual draft migration: row count changed")
        if db.exec_driver_sql(
            f"SELECT {names} FROM team_subagent_draft EXCEPT SELECT {names} FROM team_subagent_draft_v2",
        ).fetchone() is not None:
            raise RuntimeError("manual draft migration: copied row changed")
        db.exec_driver_sql("DROP TABLE team_subagent_draft")
        db.exec_driver_sql("ALTER TABLE team_subagent_draft_v2 RENAME TO team_subagent_draft")
        for statement in _INDEXES:
            db.exec_driver_sql(statement)
        if db.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("manual draft migration: foreign key check failed")
        db.exec_driver_sql("RELEASE SAVEPOINT subagent_manual_draft_expand")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT subagent_manual_draft_expand")
        db.exec_driver_sql("RELEASE SAVEPOINT subagent_manual_draft_expand")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
