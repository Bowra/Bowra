"""Persist the model id the provider actually returned on a capability probe.

The probe already compares ``payload["model"]`` against the configured model id and
fails the capability with ``model_identity_mismatch`` — but it then throws the observed
value away.  2026-08-14 on 116 that left three assignments failing with no way to tell
whether the configuration was wrong or our exact-string judgement was: the calls were
HTTP 200, and providers commonly answer with a dated/versioned id.  Without the observed
value nobody can decide, and the operator only sees "identity mismatch".

Nullable column, additive, no backfill: rows written before this revision keep NULL,
which reads as "not recorded" rather than "matched".

Revision ID: 0018_capability_evidence_actual_model
Revises: 0017_clarification_invalidation_outbox
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import context

revision = "0018_capability_evidence_actual_model"
down_revision = "0017_clarification_invalidation_outbox"
branch_labels = None
depends_on = None

_TABLES = ("team_llm_capability_evidence", "team_llm_verification_items")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for table in _TABLES:
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
        if "actual_model_id" in columns:
            # 幂等：同一 revision 可能在已升级过的库上重跑。
            continue
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN actual_model_id TEXT")


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
