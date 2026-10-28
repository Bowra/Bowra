"""Keep generated Canvas suggestions separate from human edits.

Revision ID: 0119_subagent_draft_suggestions
Revises: 0118_subagent_manual_drafts
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import context

revision = "0119_subagent_draft_suggestions"
down_revision = "0118_subagent_manual_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_subagent_draft'").fetchone() is None:
        raise RuntimeError("subagent suggestion migration: draft table missing")
    db.exec_driver_sql("""
        CREATE TABLE team_subagent_draft_suggestion (
          id TEXT PRIMARY KEY CHECK(id LIKE 'sas_%'),
          draft_id TEXT NOT NULL REFERENCES team_subagent_draft(id) ON DELETE CASCADE,
          team_id TEXT NOT NULL,
          owner_user_id TEXT NOT NULL,
          request_key_hash TEXT NOT NULL CHECK(length(request_key_hash)=64),
          base_version INTEGER NOT NULL CHECK(base_version>=1),
          kind TEXT NOT NULL CHECK(kind IN ('definition','clarification')),
          status TEXT NOT NULL CHECK(status IN ('pending','applied','dismissed')),
          payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(payload_json)<=131072),
          accepted_mutation_hash TEXT,
          accepted_version INTEGER,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          UNIQUE(team_id,owner_user_id,request_key_hash),
          CHECK((status='applied' AND accepted_mutation_hash IS NOT NULL AND accepted_version IS NOT NULL)
                OR (status!='applied' AND accepted_mutation_hash IS NULL AND accepted_version IS NULL))
        )
    """)
    db.exec_driver_sql("CREATE INDEX idx_subagent_draft_suggestion_draft ON team_subagent_draft_suggestion(draft_id,status,created_at)")
    db.exec_driver_sql("CREATE INDEX idx_subagent_draft_suggestion_expiry ON team_subagent_draft_suggestion(expires_at)")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
